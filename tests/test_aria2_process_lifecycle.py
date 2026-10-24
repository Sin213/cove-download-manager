"""A7b-1: real children, isolated sessions, bounded ownership proofs.

A2/A3/A5 are boundary controls that pass at baseline. A7 requires Windows.
Every case executes in its own disposable session, including mutation runs.
"""
import io
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest


CANARY = "A7B_SECRET_PROXY_CONF_CANARY"
ROOT = Path(__file__).resolve().parents[1]


def identity(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def alive(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


class Lab:
    def __init__(self, root):
        import cove.aria2 as aria2
        from cove.config import Settings
        self.a = aria2
        self.root, self.children, self.signals, self.reaps = root, {}, [], []
        self.bad_signals = []
        self.procs, self.partial = [], []
        self.a.STOP_TERM_TIMEOUT = self.a.STOP_KILL_TIMEOUT = 0.08
        self.a.DATA_DIR = root
        self.a.ARIA2_SESSION = root / "aria2.session"
        self.settings = Settings(rpc_secret=CANARY, proxy_host=CANARY,
                                 proxy_username=CANARY, proxy_password=CANARY)
        self.daemon = self.a.Aria2Daemon(self.settings)
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        logging.getLogger("cove").setLevel(logging.INFO)
        logging.getLogger("cove").addHandler(self.handler)
        self.sa, self.sb = self.child(), self.child(session=False)
        self.owned = None
        self.wrong_group = False

    def track(self, pid):
        assert pid and identity(pid), "test child must have a verifiable identity"
        self.children[pid] = identity(pid)
        return pid

    def child(self, ignore=False, session=True, grandchild=False, quick=False, owned=False):
        ready = self.root / f"ready-{len(self.children)}"
        body = ["import os,signal,time,subprocess,sys", "from pathlib import Path"]
        if ignore:
            body.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
        if grandchild:
            body.append(f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); Path({str(ready) + '-gc'!r}).write_text(str(p.pid))")
        body.append(f"Path({str(ready)!r}).write_text('ready')")
        body.append("time.sleep(0.02)" if quick else "time.sleep(60)")
        script = self.root / f"child-{len(self.children)}.py"
        script.write_text("\n".join(body))
        if owned:
            self.daemon._spawn([sys.executable, str(script), CANARY])
            proc = self.daemon._proc
        else:
            proc = subprocess.Popen([sys.executable, str(script)], start_new_session=session)
        self.procs.append(proc)
        self.track(proc.pid)
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.002)
        assert ready.exists(), "child failed to become ready"
        if grandchild:
            self.track(int(Path(str(ready) + "-gc").read_text()))
        return proc

    def attach(self, **kwargs):
        self.owned = self.child(owned=True, **kwargs)
        self.daemon._proc = self.owned
        return self.owned

    def check_target(self, pid, sig, group=False):
        p = self.owned
        assert p is not None and pid == p.pid, "signal target must be owned child"
        assert p.returncode is None, "never signal reaped leader"
        assert identity(pid) == self.children[pid], "no recycled PID"
        if group:
            assert not self.wrong_group, "group identity proof must reject wrong group"
            assert os.getpgid(pid) == pid != os.getpgrp(), "group target must be child session"
        self.signals.append([pid, int(sig), group, p.returncode])

    def spy(self):
        from contextlib import ExitStack
        stack = ExitStack()
        kill, killpg, send = os.kill, os.killpg, subprocess.Popen.send_signal
        def attempt(pid, sig, group=False):
            try:
                self.check_target(pid, sig, group)
            except AssertionError as exc:
                self.bad_signals.append(str(exc))
                raise
        def checked_kill(pid, sig):
            attempt(pid, sig)
            return kill(pid, sig)
        def checked_group(pid, sig):
            attempt(pid, sig, True)
            return killpg(pid, sig)
        def checked_send(p, sig):
            attempt(p.pid, sig)
            return send(p, sig)
        stack.enter_context(patch.object(os, "kill", checked_kill))
        stack.enter_context(patch.object(os, "killpg", checked_group))
        stack.enter_context(patch.object(subprocess.Popen, "send_signal", checked_send))
        return stack

    def construct(self, exc=None, reaped=False, missing=False, prespawn=False):
        """Real fork, then inject at exec-status read AFTER Popen acquires pid.

        Raising inside _fork_exec itself would precede self.pid assignment,
        making ownership unprovable. No test assigns a production pid.
        """
        cls = getattr(self.a, "_OwnedPopen", subprocess.Popen)
        execute, fork, read, waitpid = (subprocess.Popen._execute_child,
                                      subprocess._fork_exec, os.read, os.waitpid)
        armed = []
        def capture(p, *args, **kwargs):
            self.partial.append(p)
            return execute(p, *args, **kwargs)
        def real_fork(*args):
            pid = fork(*args)
            self.track(pid)
            armed.append(args[12])
            return pid
        def injected_read(fd, count):
            if armed and fd == armed[0] and exc:
                p = self.partial[-1]
                assert p.pid in self.children, "normal Popen assignment must establish ownership"
                self.owned = p
                if reaped:
                    p.wait(timeout=2)
                raise exc
            return read(fd, count)
        def reaper(pid, flags):
            result = waitpid(pid, flags)
            if result[0]:
                self.reaps.append(result[0])
            return result
        script = self.root / "constructor-child.py"
        script.write_text("import time; time.sleep(0.01)" if reaped else "import time; time.sleep(60)")
        args = [sys.executable, str(script)]
        if missing:
            args = [str(self.root / "does-not-exist")]
        if prespawn:
            args = ["invalid\0argument"]
        with patch.object(subprocess.Popen, "_execute_child", capture), \
             patch.object(subprocess, "_fork_exec", real_fork), \
             patch.object(os, "read", injected_read), patch.object(os, "waitpid", reaper):
            self.daemon._spawn(args)
        self.owned = self.daemon._proc
        self.procs.append(self.owned)
        return self.owned

    def finish(self):
        # The guards are removed before cleanup, but PID identity remains mandatory.
        try:
            assert alive(self.sa.pid) and alive(self.sb.pid), "sentinels must survive"
            text = self.stream.getvalue()
            assert CANARY not in text, "lifecycle logs must not contain credentials or paths"
            print(json.dumps({"signals": self.signals, "reaps": self.reaps,
                              "records": text, "children": list(self.children),
                              "rejected_signals": self.bad_signals}), flush=True)
            assert not self.bad_signals, "production attempted an unsafe signal"
        finally:
            logging.getLogger("cove").removeHandler(self.handler)
            for pid, start in self.children.items():
                if identity(pid) == start and alive(pid):
                    os.kill(pid, signal.SIGKILL)
            for p in self.procs + self.partial:
                if p.pid:
                    p.wait(timeout=2)
            deadline = time.monotonic() + 2
            while any(alive(pid) for pid in self.children) and time.monotonic() < deadline:
                time.sleep(0.005)
            assert not any(alive(pid) for pid in self.children), "test must leave no live child"


def case_a1(lab, interrupt=False):
    original = KeyboardInterrupt(CANARY) if interrupt else RuntimeError(CANARY)
    with lab.spy():
        try:
            lab.construct(original)
        except BaseException as exc:
            assert exc is original, "preserve original BaseException identity"
        else:
            assert False, "injected constructor exception must propagate"
    p = lab.partial[-1]
    assert not alive(p.pid), "post-fork exception must not leak real child"
    assert p.returncode is not None, "child must be positively reaped"
    assert lab.reaps.count(p.pid) == 1, "exactly one successful reap"
    assert not getattr(lab.a, "_owned_pending", None)
    with pytest.raises(ChildProcessError):
        os.waitpid(p.pid, os.WNOHANG)


def case_a2(lab):
    # Boundary: baseline passes; CPython already reaps exec failures.
    with lab.spy(), pytest.raises(lab.a.Aria2Error):
        lab.construct(missing=True)
    assert lab.partial[-1].returncode is not None
    assert not lab.signals and not getattr(lab.a, "_owned_pending", None)


def case_a3(lab):
    # Boundary: baseline passes; no child was created.
    with lab.spy(), pytest.raises(ValueError):
        lab.construct(prespawn=True)
    assert lab.partial[-1].pid is None and not lab.signals
    assert not getattr(lab.a, "_owned_pending", None), "pid=None must never enter registry"
    assert set(lab.children) == {lab.sa.pid, lab.sb.pid}


def case_a4(lab, wrong=True):
    lab.wrong_group = wrong
    original = RuntimeError("post fork")
    getpgid = os.getpgid
    def group(pid):
        return os.getpgrp() if wrong else getpgid(pid)
    with lab.spy(), patch.object(os, "getpgid", group):
        with pytest.raises(RuntimeError, match="post fork"):
            lab.construct(original)
    assert not alive(lab.owned.pid), "constructor cleanup must reap"
    assert any(s[2] for s in lab.signals) is (not wrong)


def case_a5(lab):
    # Boundary: baseline passes; inject only after the actual child is reaped.
    with lab.spy(), pytest.raises(RuntimeError, match="already reaped"):
        lab.construct(RuntimeError("already reaped"), reaped=True)
    assert lab.partial[-1].returncode == 0 and not lab.signals


def refusal(lab):
    before = set(lab.root.iterdir())
    with patch.object(lab.daemon, "_spawn") as spawn, \
         patch.object(lab.daemon, "_write_rpc_conf") as write, \
         patch.object(lab.daemon, "_port_in_use", return_value=False), \
         patch.object(lab.daemon, "_await_rpc", return_value=lab.a.Aria2Error("not ready")), \
         patch.object(lab.a, "_resolve_aria2c", return_value=sys.executable):
        with pytest.raises(lab.a.Aria2Error, match="^Aria2 child cleanup is unresolved\\.$"):
            lab.daemon.start()
        spawn.assert_not_called()
        write.assert_not_called()
    assert set(lab.root.iterdir()) == before


def case_a6(lab):
    original = KeyboardInterrupt("original")
    with lab.spy(), patch.object(os, "killpg", side_effect=PermissionError(CANARY)), \
         patch.object(subprocess.Popen, "kill", side_effect=PermissionError(CANARY)):
        with pytest.raises(KeyboardInterrupt) as caught:
            lab.construct(original)
    assert caught.value is original
    p = lab.partial[-1]
    assert alive(p.pid) and getattr(lab.a, "_owned_pending", None) is p
    refusal(lab)
    # A direct second constructor must refuse too, without overwriting the slot.
    with patch.object(subprocess, "_fork_exec", side_effect=AssertionError("second fork forbidden")) as fork:
        with pytest.raises(lab.a.Aria2Error):
            lab.a._OwnedPopen([sys.executable, "-c", "pass"], start_new_session=True)
        fork.assert_not_called()
    assert lab.a._owned_pending is p
    with lab.spy():
        assert lab.daemon.stop() is True
        assert lab.daemon.stop() is True
    assert lab.a._owned_pending is None and not alive(p.pid)
    assert lab.stream.getvalue().count("aria2_child_unresolved") == 1


def case_b1(lab):
    p = lab.attach(quick=True)
    p.wait(timeout=2)
    with lab.spy():
        assert lab.daemon.stop() is True
        assert lab.daemon.stop() is True
    assert lab.daemon._proc is None and not lab.signals


def case_b2(lab):
    p = lab.attach()
    with lab.spy():
        assert lab.daemon.stop() is True
    assert p.returncode == -signal.SIGTERM
    assert not any(s[1] == signal.SIGKILL for s in lab.signals)


def case_b3(lab):
    p = lab.attach(ignore=True, grandchild=True)
    started = time.monotonic()
    with lab.spy():
        assert lab.daemon.stop() is True
    assert p.returncode == -signal.SIGKILL
    assert any(s[2] for s in lab.signals), "proven group must be killed"
    assert time.monotonic() - started < 0.8, "scaled bounded escalation"
    deadline = time.monotonic() + 1
    while any(alive(pid) for pid in lab.children if pid not in (lab.sa.pid, lab.sb.pid)) and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not any(alive(pid) for pid in lab.children if pid not in (lab.sa.pid, lab.sb.pid)), "descendant must die"


def case_b5(lab):
    p = lab.attach(ignore=True)
    wait = p.wait
    def exited(timeout=None):
        os.kill(p.pid, signal.SIGKILL)
        wait(timeout=2)
        raise subprocess.TimeoutExpired("redacted", timeout)
    # Simulate recycled group lookup too: returncode, not identity lookup, is
    # the authority once the owned leader has been reaped.
    with lab.spy(), patch.object(p, "wait", exited), patch.object(os, "getpgid", return_value=p.pid):
        assert lab.daemon.stop() is True
    assert p.returncode == -signal.SIGKILL


def case_b6(lab, waits=False):
    p = lab.attach(ignore=True)
    lab.daemon._secret = lab.daemon._active_secret = CANARY
    conf = lab.root / CANARY
    conf.write_text(CANARY)
    lab.daemon._launch_conf = conf
    def timeout(timeout=None):
        assert timeout is not None, "every wait must be bounded"
        raise subprocess.TimeoutExpired(CANARY, timeout)
    with lab.spy(), patch.object(os, "killpg", side_effect=PermissionError(CANARY)), \
         patch.object(p, "kill", side_effect=PermissionError(CANARY)), \
         patch.object(p, "wait", timeout):
        assert lab.daemon.stop() is False, "failed cleanup must report False"
        assert lab.daemon._proc is p and lab.daemon._unreleased
        assert lab.daemon.stop() is False
        refusal(lab)  # before healthy-child early return
    assert alive(p.pid) and p.poll() is None
    assert not conf.exists() and lab.daemon._secret is lab.daemon._active_secret is None
    text = lab.stream.getvalue()
    assert text.count("aria2_child_unresolved") == 1
    assert f"pid={p.pid}" in text and "phase=kill" in text and f"platform={sys.platform}" in text
    with lab.spy():
        assert lab.daemon.stop() is True
        count = len(lab.signals)
        assert lab.daemon.stop() is True
        assert len(lab.signals) == count, "no recycled PID signalling after release"
    assert lab.daemon._proc is None and not lab.daemon._unreleased
    with patch.object(lab.a, "_resolve_aria2c", return_value=None):
        with pytest.raises(lab.a.Aria2Error, match="aria2c not found"):
            lab.daemon.start()  # recovery removes refusal


def case_b9(lab):
    with lab.spy():
        assert lab.daemon.stop() is True
        assert lab.daemon.stop() is True
    assert not lab.signals


def case_b12(lab):
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    p = lab.attach(quick=True)
    time.sleep(0.06)
    assert p.poll() == 0  # CPython ECHILD means gone, status unavailable.
    with lab.spy():
        assert lab.daemon.stop() is True
    assert "status-unavailable" in lab.stream.getvalue()


def case_c3(lab):
    # Real start() failure, not a mocked stop() or fake Popen.
    lab.settings.proxy_type = "none"
    lab.a._resolve_aria2c = lambda: sys.executable
    lab.daemon._port_in_use = lambda: False
    def spawn(args):
        with patch.object(lab.daemon, "_spawn", lab.a.Aria2Daemon._spawn.__get__(lab.daemon)):
            lab.attach(ignore=True)
    lab.daemon._spawn = spawn
    lab.daemon._await_rpc = lambda: lab.a.Aria2Error("auth failed")
    with lab.spy(), patch.object(os, "killpg", side_effect=PermissionError(CANARY)), \
         patch.object(subprocess.Popen, "kill", side_effect=PermissionError(CANARY)):
        with pytest.raises(lab.a.Aria2Error, match="aria2 RPC did not come up"):
            lab.daemon.start()
    assert lab.daemon._proc is lab.owned and lab.daemon._unreleased
    refusal(lab)


def case_c4(lab):
    # Deterministic interleaving: first read captures the child and clears slot.
    p = lab.attach()
    class Racing(type(lab.daemon)):
        def __getattribute__(self, name):
            value = super().__getattribute__(name)
            if name == "_proc" and value is not None:
                object.__setattr__(self, "_proc", None)
            return value
    lab.daemon.__class__ = Racing
    try:
        result = lab.daemon.is_running()
    except AttributeError:
        pytest.fail("is_running must capture the handle once")
    assert result is True and p.poll() is None


def case_b_poll(lab):
    p = lab.attach(ignore=True)
    with lab.spy(), patch.object(p, "poll", side_effect=OSError(CANARY)):
        assert lab.daemon.stop() is False
    assert lab.daemon._proc is p and lab.daemon._unreleased


def case_b_term(lab):
    p = lab.attach()
    send = p.send_signal
    def term_fails(sig):
        if sig == signal.SIGTERM:
            raise OSError(CANARY)
        return send(sig)
    with lab.spy(), patch.object(p, "terminate", side_effect=OSError(CANARY)), \
         patch.object(p, "send_signal", term_fails):
        assert lab.daemon.stop() is True
    assert p.returncode == -signal.SIGKILL


def case_wait_evidence(lab):
    # B7: delivered requests and timeout exceptions cannot certify exit.
    p = lab.attach(ignore=True)
    waits = []
    def pending(timeout=None):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("redacted", timeout)
    def delivered(pid, sig):
        lab.check_target(pid, sig, True)  # delivery is deliberately ineffective
    with lab.spy(), patch.object(p, "terminate"), patch.object(p, "wait", pending), \
         patch.object(os, "killpg", delivered):
        assert lab.daemon.stop() is False
        assert lab.daemon._proc is p and p.poll() is None
        assert waits == [0.08, 0.08], "TERM and KILL waits must each have a timeout"
        assert lab.daemon.stop(kill_only=True) is False
        assert waits == [0.08, 0.08, 0.08], "retry must use kill phase only"
    with lab.spy():
        assert lab.daemon.stop() is True


def case_a6_wait(lab):
    # Constructor has a usable handle but no exit evidence after delivered kill.
    original = RuntimeError("original construction failure")
    waits = []
    def pending(p, timeout=None):
        waits.append(timeout)
        raise subprocess.TimeoutExpired("redacted", timeout)
    def delivered(pid, sig):
        lab.check_target(pid, sig, True)
    with lab.spy(), patch.object(subprocess.Popen, "wait", pending), \
         patch.object(os, "killpg", delivered):
        with pytest.raises(RuntimeError) as caught:
            lab.construct(original)
    assert caught.value is original and waits == [0.08]
    assert lab.a._owned_pending is lab.owned and alive(lab.owned.pid)
    refusal(lab)
    with lab.spy():
        assert lab.daemon.stop() is True
    assert lab.a._owned_pending is None


def case_no_group_identity(lab):
    p = lab.attach(ignore=True)
    with lab.spy(), patch.object(os, "getpgid", side_effect=OSError(CANARY)):
        assert lab.daemon.stop() is False
    assert lab.daemon._proc is p and alive(p.pid)
    assert not any(s[1] == signal.SIGKILL for s in lab.signals), "unavailable identity is unresolved"


def case_no_session_request(lab):
    # Popen methods are safe for a child that shares the runner's group.
    p = lab.child(session=False)
    lab.owned = lab.daemon._proc = p
    with lab.spy():
        assert lab.daemon.stop(kill_only=True) is True
    assert p.returncode == -signal.SIGKILL and not any(s[2] for s in lab.signals)


def case_negative_safety(lab):
    # E1/B10 negative controls prove guard rejection without delivering a signal.
    p = lab.attach()
    for target in (lab.sa.pid, lab.sb.pid, os.getpid()):
        with pytest.raises(AssertionError, match="owned child"):
            lab.check_target(target, signal.SIGKILL, True)
    lab.wrong_group = True
    with pytest.raises(AssertionError, match="wrong group"):
        lab.check_target(p.pid, signal.SIGKILL, True)
    lab.wrong_group = False
    p.terminate()
    p.wait(timeout=2)
    with pytest.raises(AssertionError, match="reaped leader"):
        lab.check_target(p.pid, signal.SIGKILL)
    lab.stream.write(CANARY)
    assert CANARY in lab.stream.getvalue(), "canary detection positive control"
    lab.stream.seek(0)
    lab.stream.truncate()


def case_control(lab):
    p = lab.construct()
    assert p.poll() is None and lab.daemon.is_running()


def case_r1(lab):
    old = lab.attach(quick=True)
    old.wait(timeout=2)
    original, calls = RuntimeError("replacement constructor"), []
    real_spawn, real_stop = lab.daemon._spawn, lab.daemon.stop
    def replacement(args):
        with patch.object(lab.daemon, "_spawn", real_spawn):
            lab.construct(original)
    def stop(**kwargs):
        result = real_stop(**kwargs)
        calls.append(result)
        return result
    lab.settings.proxy_type = "none"
    with lab.spy(), patch.object(lab.a, "_resolve_aria2c", return_value=sys.executable), \
         patch.object(lab.daemon, "_port_in_use", return_value=False), \
         patch.object(lab.daemon, "_spawn", replacement), patch.object(lab.daemon, "stop", stop), \
         patch.object(os, "killpg", side_effect=PermissionError("controlled")), \
         patch.object(subprocess.Popen, "kill", side_effect=PermissionError("controlled")):
        with pytest.raises(RuntimeError) as caught:
            lab.daemon.start()
        assert caught.value is original
        assert calls == [False], "an exited old handle must not mask the pending live child"
        assert lab.daemon._unreleased and alive(lab.a._owned_pending.pid)
        assert not lab.daemon.is_running(), "pending construction is not a healthy daemon"
        refusal(lab)
        assert lab.daemon.stop(kill_only=True) is False
    with lab.spy():
        assert lab.daemon.stop(kill_only=True) is True
        assert lab.daemon.stop() is True
    assert lab.a._owned_pending is None and lab.daemon._proc is None
    assert not lab.daemon._unreleased
    assert lab.stream.getvalue().count("aria2_child_unresolved") == 1


def case_r2(lab):
    p, kill = lab.attach(ignore=True), os.kill
    entered, attempted, done = threading.Event(), threading.Event(), threading.Event()
    observed, errors = [], []
    def reaper():
        try:
            assert entered.wait(1)
            attempted.set()
            lab.daemon.active_rpc_secret()
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()
    worker = threading.Thread(target=reaper)
    def sink(pid, sig):
        lab.check_target(pid, sig, True)
        kill(pid, sig)  # Leader exits but remains unreaped until the worker polls.
        deadline = time.monotonic() + 1
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.002)
        assert not alive(pid)
        entered.set()
        assert attempted.wait(1)
        done.wait(0.15)  # A serialized reaper stays blocked until this sink returns.
        observed.append(p.returncode)
    worker.start()
    try:
        with lab.spy(), patch.object(os, "killpg", sink):
            assert lab.daemon.stop() is True
    finally:
        entered.set()
        worker.join(1)
    assert not worker.is_alive() and not errors, "reaper must finish without deadlock"
    assert observed == [None], "signal sink must never observe an already reaped leader"
    # Waits must release the module lock so ordinary worker probes stay cheap.
    p = lab.attach(ignore=True)
    wait, probed, probes, workers = p.wait, threading.Event(), [], []
    def bounded_wait(timeout=None):
        worker = threading.Thread(target=lambda: (lab.daemon.is_running(), probed.set()))
        workers.append(worker)
        worker.start()
        try:
            probes.append(probed.wait(0.3))
        finally:
            worker.join(0.5)
        return wait(timeout=timeout)
    with lab.spy(), patch.object(p, "wait", bounded_wait):
        assert lab.daemon.stop() is True
    for worker in workers:
        worker.join(0.5)
    assert all(probes), "module lock must not span a bounded wait"


def case_r3(lab, diagnostic=False, interrupt=False):
    original = KeyboardInterrupt("original") if interrupt else RuntimeError("original")
    injected = KeyboardInterrupt("handler") if interrupt else RuntimeError("handler")
    registered = []
    def release(p, **kwargs):
        registered.append(lab.a._owned_pending is p)
        if diagnostic:
            return False
        raise injected
    def emit(*args, **kwargs):
        raise injected
    logger = logging.getLogger("cove")
    with lab.spy(), patch.object(lab.a, "_release_child", release), \
         patch.object(logger, "error", emit if diagnostic else logger.error):
        try:
            lab.construct(original)
        except BaseException as caught:
            assert caught is original, "handler failure must preserve the original exception object"
        else:
            assert False, "original constructor exception must propagate"
    assert registered == [True], "ownership must be registered before the first release hook"
    p = lab.owned
    assert lab.a._owned_pending is p and alive(p.pid)
    refusal(lab)
    if diagnostic:
        assert not getattr(p, "_cove_unresolved_logged", False), "failed emission must remain retryable"
        with lab.spy(), patch.object(lab.a, "_release_child", return_value=False):
            assert lab.daemon.stop() is False
    with lab.spy():
        assert lab.daemon.stop() is True
    assert lab.a._owned_pending is None and not alive(p.pid)
    assert lab.stream.getvalue().count("aria2_child_unresolved") == 1


def case_r4(lab, phase="poll"):
    p = lab.attach(ignore=True)
    def failed_kill(pid, sig):
        lab.check_target(pid, sig, True)
        raise PermissionError("controlled")
    target, name, failure = ((p, "poll", OSError("controlled")) if phase == "poll" else
                             (os, "getpgid", OSError("controlled")) if phase == "term" else
                             (os, "killpg", failed_kill))
    with lab.spy(), patch.object(target, name, side_effect=failure):
        assert lab.daemon.stop() is False
    assert f"phase={phase}" in lab.stream.getvalue(), "diagnostic must name the last reached phase"
    if phase == "poll":
        assert not lab.signals, "initial probe failure must send no signal"
    else:
        assert any(s[1] == signal.SIGTERM for s in lab.signals)
        assert any(s[1] == signal.SIGKILL for s in lab.signals) is (phase == "kill")


def case_r3_released(lab):
    release = lab.a._release_child
    for injected in (RuntimeError("handler"), KeyboardInterrupt("handler")):
        original = RuntimeError("original")
        def after_reap(p, **kwargs):
            assert lab.a._owned_pending is p
            assert release(p)
            raise injected
        with lab.spy(), patch.object(lab.a, "_release_child", after_reap):
            with pytest.raises(RuntimeError) as caught:
                lab.construct(original)
        assert caught.value is original
        assert lab.a._owned_pending is None and not alive(lab.owned.pid)
    assert "aria2_child_unresolved" not in lab.stream.getvalue()


def case_r3_start(lab, diagnostic=False, interrupt=False):
    original = RuntimeError("original start construction")
    injected = KeyboardInterrupt("cleanup") if interrupt else RuntimeError("cleanup")
    spawn, logger = lab.daemon._spawn, logging.getLogger("cove")
    def replacement(args):
        with patch.object(lab.daemon, "_spawn", spawn):
            lab.construct(original)
    def release(p, **kwargs):
        assert lab.a._owned_pending is p
        if diagnostic:
            return False
        raise injected
    lab.settings.proxy_type = "none"
    with lab.spy(), patch.object(lab.a, "_resolve_aria2c", return_value=sys.executable), \
         patch.object(lab.daemon, "_port_in_use", return_value=False), \
         patch.object(lab.daemon, "_spawn", replacement), patch.object(lab.a, "_release_child", release), \
         patch.object(logger, "error", side_effect=injected) if diagnostic else patch.object(logger, "error", wraps=logger.error):
        try:
            lab.daemon.start()
        except BaseException as caught:
            assert caught is original, "start cleanup must preserve the constructor exception"
        else:
            assert False, "start must propagate the original exception"
        assert lab.a._owned_pending is lab.owned and alive(lab.owned.pid)
        if diagnostic:
            assert lab.daemon.stop(kill_only=True) is False, "diagnostic failure must not abort stop"
    refusal(lab)
    with lab.spy(), patch.object(lab.a, "_release_child", return_value=False):
        assert lab.daemon.stop() is False
    with lab.spy():
        assert lab.daemon.stop() is True
    assert lab.a._owned_pending is None
    assert lab.stream.getvalue().count("aria2_child_unresolved") == 1


CASES = {"a1": case_a1, "a1_interrupt": lambda l: case_a1(l, True),
         "a2": case_a2, "a3": case_a3, "a4": case_a4,
         "a4_proven": lambda l: case_a4(l, False), "a5": case_a5, "a6": case_a6,
         "a1_control": case_control, "b1": case_b1, "b2": case_b2,
         "b3": case_b3, "b5": case_b5, "b6": case_b6,
         "b7": lambda l: case_b6(l, True), "b9": case_b9, "b12": case_b12,
         "c3": case_c3, "c4": case_c4, "b_poll": case_b_poll, "b_term": case_b_term,
         "wait_evidence": case_wait_evidence, "a6_wait": case_a6_wait,
         "no_group_identity": case_no_group_identity, "no_session": case_no_session_request,
         "negative_safety": case_negative_safety, "r1": case_r1, "r2": case_r2,
         "r3_release": case_r3, "r3_release_interrupt": lambda l: case_r3(l, False, True),
         "r3_log": lambda l: case_r3(l, True), "r3_log_interrupt": lambda l: case_r3(l, True, True),
         "r4_poll": case_r4, "r4_term": lambda l: case_r4(l, "term"),
         "r4_kill": lambda l: case_r4(l, "kill"), "r3_released": case_r3_released,
         "r3_start_release": case_r3_start,
         "r3_start_release_interrupt": lambda l: case_r3_start(l, False, True),
         "r3_start_log": lambda l: case_r3_start(l, True),
         "r3_start_log_interrupt": lambda l: case_r3_start(l, True, True)}


@pytest.mark.skipif(sys.platform != "linux", reason="Arch real-process proof; native proof pending")
@pytest.mark.parametrize("case", CASES)
def test_lifecycle(case, tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT), HOME=str(tmp_path),
               XDG_DATA_HOME=str(tmp_path / "data"), XDG_CONFIG_HOME=str(tmp_path / "config"))
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), case, str(tmp_path)],
                            start_new_session=True, capture_output=True, text=True,
                            env=env, timeout=8)
    print(result.stdout, end="")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="A7 AWAITING NATIVE PROOF (R-WIN-1)")
def test_a7_windows_handle_guard():
    import inspect
    import cove.aria2 as aria2
    source = inspect.getsource(aria2._OwnedPopen)
    assert 'getattr(self, "_handle", None)' in source


if __name__ == "__main__":
    # Fail closed BEFORE any children or group-signalling production call.
    assert os.getsid(0) == os.getpgid(0) == os.getpid(), "runner must own disposable session"
    def watchdog(*args):
        raise AssertionError("hard-cap lifecycle watchdog expired")
    signal.signal(signal.SIGALRM, watchdog)
    signal.alarm(6)
    lab = Lab(Path(sys.argv[2]))
    try:
        CASES[sys.argv[1]](lab)
    finally:
        lab.finish()
        signal.alarm(0)
