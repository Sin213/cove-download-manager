"""Synthetic S08D boundaries; no native daemon or network is used.

Ownership is name-based, not provenance. The lstat/unlink race affects one
entry; other RPC ports and later binds are undetectable by this port check.
Hard-linked copies, backups, snapshots and free space are not addressed.
"""
import builtins
import errno
import hashlib
import io
import logging
import os
import sqlite3
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from cove import aria2, config, db
from tests.test_aria2_external import ScriptedRPC

PAYLOAD = b"http://x.invalid/file?SYNTH_TOKEN=SYNTH_PASSKEY"


def data(path): return path.read_bytes() if path.is_file() else None


@pytest.fixture
def s(tmp_path, monkeypatch, caplog):
    data = tmp_path / "cove-app-data" / "cove-download-manager"; data.mkdir(parents=True)
    s = SimpleNamespace(data=data, path=data / "aria2.session", creates=[], events=[], opens=[], in_start=False,
                        lstats=0, lstat_effect=None, unlink_effect=None, root=tmp_path)
    for module in (aria2, config):
        for name, value in (("DATA_DIR", data), ("ARIA2_SESSION", s.path),
                            ("ARIA2_LOG", data / "aria2.log")):
            monkeypatch.setattr(module, name, value)
    for module in (config, db):
        monkeypatch.setattr(module, "DB_FILE", data / "cove.db")
    monkeypatch.setattr(db, "DATA_DIR", data, raising=False)
    s.daemon = aria2.Aria2Daemon(config.Settings(download_dir=str(tmp_path / "downloads"),
                                               rpc_secret="SYNTH_TOKEN"))
    proc = MagicMock(); proc.poll.return_value = None
    s.popen = MagicMock(side_effect=lambda *a, **k: s.events.append("spawn") or proc)
    monkeypatch.setattr(aria2.subprocess, "Popen", s.popen)
    monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: "synthetic-aria2c")
    ScriptedRPC(monkeypatch, data)
    monkeypatch.setattr(aria2, "interface_exists", lambda name: True)
    s.connect_error = ConnectionRefusedError(errno.ECONNREFUSED, "synthetic refusal")
    def connect(address):
        if s.connect_error is not None: raise s.connect_error
    fake = MagicMock(); fake.__enter__.return_value = fake
    fake.connect.side_effect = connect; fake.close.return_value = None; fake.__exit__.side_effect = lambda *a: fake.close()
    monkeypatch.setattr(aria2, "socket", SimpleNamespace(socket=lambda: fake))
    probe = s.daemon._port_in_use
    monkeypatch.setattr(s.daemon, "_port_in_use", lambda: s.events.append("port") or probe())
    s.reclaim = MagicMock(); monkeypatch.setattr(s.daemon, "_reclaim_stale_daemon", s.reclaim)
    caplog.set_level(logging.INFO, logger="cove")
    touch, lstat, unlink, osopen = Path.touch, Path.lstat, Path.unlink, os.open
    def scoped_lstat(path, *a, **k):
        if path == s.path:
            s.lstats += 1
            if s.lstat_effect: return s.lstat_effect()
        return lstat(path, *a, **k)
    def scoped_unlink(path, *a, **k):
        if path == s.path:
            s.events.append("unlink")
            if s.unlink_effect: return s.unlink_effect()
        return unlink(path, *a, **k)
    def scoped_touch(path, *a, **k):
        # Record without executing HEAD's touch: directories/FIFOs must reach assertions.
        if path == s.path: s.creates.append("touch"); return None
        return touch(path, *a, **k)
    def scoped_osopen(path, flags, *a, **k):
        if not isinstance(path, int) and Path(path) == s.path:
            if s.in_start: s.opens.append("os.open")
            if flags & os.O_CREAT: s.creates.append("os.open")
        return osopen(path, flags, *a, **k)
    def spy_open(original):
        def opened(path, mode="r", *a, **k):
            if not isinstance(path, int) and Path(path) == s.path:
                if s.in_start: s.opens.append("open")
                if any(c in mode for c in "wax+"): s.creates.append("open")
            return original(path, mode, *a, **k)
        return opened
    monkeypatch.setattr(Path, "touch", scoped_touch); monkeypatch.setattr(Path, "lstat", scoped_lstat)
    monkeypatch.setattr(Path, "unlink", scoped_unlink); monkeypatch.setattr(os, "open", scoped_osopen)
    monkeypatch.setattr(builtins, "open", spy_open(builtins.open)); monkeypatch.setattr(io, "open", spy_open(io.open))
    s.real_lstat, s.real_unlink = lstat, unlink; original_start = s.daemon.start
    def checked_start():
        s.in_start, s.opens = True, []
        try: original_start()
        finally: s.in_start = False
        # Check normal returns without masking expected startup exceptions.
        assert not s.opens, "start() must not open the session file"
    monkeypatch.setattr(s.daemon, "start", checked_start); return s


def records(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "cove"]


def skipped(caplog, reason):
    assert records(caplog) == [("INFO", f"legacy_aria2_session_cleanup_skipped: reason={reason}")]


def fail(exc):
    def raised(): raise exc
    return raised


@pytest.mark.parametrize("case", ["A1", "A2", "A3", "A4"], ids=str)
def test_A1_A2_A3_A4_no_creation(s, monkeypatch, case):
    if case == "A2": s.popen.side_effect = OSError(errno.EACCES, "synthetic")
    if case == "A3":
        s.daemon.settings.torrent_network_interface = "synthetic-missing"
        monkeypatch.setattr(aria2, "interface_exists", lambda name: False)
    if case == "A4": monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: None)
    if case != "A1":
        error, message = (aria2.Aria2InterfaceError, "was not found") if case == "A3" else (
            aria2.Aria2Error, "Failed to launch aria2c" if case == "A2" else "aria2c not found")
        with pytest.raises(error, match=message): s.daemon.start()
    else: s.daemon.start()
    assert s.creates == [], f"{case}: start must never create/touch the session"
    assert not s.path.exists()
    if case == "A1":
        assert {p.name for p in s.data.iterdir()} == {"aria2.rpc.auth"}
        if os.name != "nt": assert stat.S_IMODE((s.data / "aria2.rpc.auth").stat().st_mode) == 0o600


@pytest.mark.parametrize("test_id,prefix", [("B1", "--save-session"), ("C1", "--save-session-interval"),
                                           ("B2", None)], ids=["B1", "C1", "B2"])
@pytest.mark.parametrize("proxy", [False, True], ids=["proxy-off", "proxy-on"])
def test_B1_C1_B2_argv(s, test_id, prefix, proxy):
    settings = s.daemon.settings
    settings.proxy_type, settings.proxy_host = ("http", "127.0.0.1") if proxy else ("none", "")
    settings.torrent_network_interface = "synthetic-iface"
    settings.speed_limiter_enabled, settings.overall_speed_limit_kbps = True, 123
    s.daemon.start(); argv = s.popen.call_args.args[0]
    assert "--continue=true" in argv and any(a.startswith("--conf-path=") for a in argv)
    forbidden = (prefix,) if prefix else ("--input-file", "-i", "--deferred-input", "--force-save")
    assert not any(a.split("=", 1)[0] in forbidden if prefix is None else a.startswith(prefix)
                   for a in argv), f"{test_id}: forbidden session argv"


def test_D1_database_untouched(s, monkeypatch):
    path = s.data / "cove.db"; path.write_bytes(PAYLOAD)
    before = hashlib.sha256(path.read_bytes()).digest()
    def forbidden(*a, **k): pytest.fail("D1: start opened the database")
    monkeypatch.setattr(db, "connect", forbidden); monkeypatch.setattr(sqlite3, "connect", forbidden)
    s.daemon.start()
    after = data(path)
    assert after is not None and hashlib.sha256(after).digest() == before, "D1: database modified"


@pytest.mark.parametrize("optional", [False, True], ids=["D2-base", "D2-optional"])
def test_D2_add_uri_options(s, monkeypatch, optional):
    rpc = aria2.Aria2RPC(s.daemon.settings); call = MagicMock(return_value="gid")
    monkeypatch.setattr(rpc, "_call", call)
    extra = dict(filename="x.bin", gid="123456789abcdef0", speed_limit_kbps=12) if optional else {}
    rpc.add_uri(["http://x.invalid/file"], str(s.root), 4, **extra)
    expected = {"dir": str(s.root), "split": "4", "max-connection-per-server": "4", "continue": "true"}
    if optional: expected.update(out="x.bin", gid="123456789abcdef0", **{"max-download-limit": "12K"})
    call.assert_called_once_with("aria2.addUri", [["http://x.invalid/file"], expected])


@pytest.mark.parametrize("case", ["E1", "F1", "F3", "J1", "K1", "K2", "I5-fresh", "I5-old"], ids=str)
def test_E1_F1_F3_J1_K1_K2_I5_removal(s, monkeypatch, case):
    s.path.write_bytes(b"arbitrary non-session bytes" if case == "J1" else PAYLOAD)
    siblings = []
    if case == "E1":
        siblings = [s.data / p for p in ("x.bin.aria2", "aria2.log", "abc.torrent", "cove.db",
                                        "aria2.session.bak", "downloads/aria2.session")]
        siblings.append(s.root / "downloads/x.bin.aria2")
    if case == "K2": siblings = [s.root / "other-data/aria2.session"]
    for path in siblings: path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(PAYLOAD)
    if case == "F3":
        sibling = s.data / "other.keep"; os.link(s.path, sibling); siblings.append(sibling)
    if case.startswith("I5"):
        age = 1 if case.endswith("fresh") else 3600
        os.utime(s.path, (time.time() - age, time.time() - age))
    s.daemon.start()
    assert all(data(p) == PAYLOAD for p in siblings), f"{case}: sibling changed"
    assert not s.path.exists(), f"{case}: eligible exact session name must be removed"
    if case == "K2":
        s.path = siblings[0]; monkeypatch.setattr(aria2, "ARIA2_SESSION", s.path)
        s.daemon._proc = None; s.daemon.start()
        assert data(s.path) == PAYLOAD, "K2: mismatched parent must survive"


@pytest.mark.parametrize("case", ["F4", "F5"], ids=str)
def test_F4_F5_exact_path_guards(s, monkeypatch, case):
    s.path = s.root / "aria2.session" if case == "F4" else s.data / "other.keep"
    s.path.write_bytes(PAYLOAD); monkeypatch.setattr(aria2, "ARIA2_SESSION", s.path)
    s.daemon.start()
    assert data(s.path) == PAYLOAD, f"{case}: ineligible configured path changed"


@pytest.mark.parametrize("case", ["G1", "G2", "G3", "G4", "G5"], ids=str)
def test_G1_G2_G3_G4_G5_skips(s, monkeypatch, caplog, case):
    decoy = s.root / "decoy"; decoy.write_bytes(PAYLOAD)
    if case in ("G1", "G2"):
        if case == "G2": decoy.unlink()
        s.path.symlink_to(decoy)
    elif case == "G3": s.path.mkdir(); (s.path / "child").write_bytes(PAYLOAD)
    elif case == "G4":
        if not hasattr(os, "mkfifo"): pytest.skip("FIFO creation is unavailable on this platform")
        os.mkfifo(s.path)
    else:
        s.path.write_bytes(PAYLOAD)
        monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)
        s.lstat_effect = lambda: SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)
    s.daemon.start()
    assert os.path.lexists(s.path), f"{case}: skipped entry removed"
    if case == "G1": assert s.path.is_symlink() and data(decoy) == PAYLOAD, "G1: symlink or decoy changed"
    if case == "G2": assert s.path.is_symlink() and not decoy.exists()
    if case == "G3": assert data(s.path / "child") == PAYLOAD, "G3: directory child changed"
    if case == "G4": assert stat.S_ISFIFO(s.real_lstat(s.path).st_mode)
    if case == "G5": assert data(s.path) == PAYLOAD, "G5: reparse file changed"
    skipped(caplog, "reparse_point" if case == "G5" else "not_regular_file")


@pytest.mark.parametrize("case", ["F2", "H1", "H2", "H3", "H4", "H5", "J2", "J3"], ids=str)
def test_F2_H1_H2_H3_H4_H5_J2_J3_errors_and_races(s, caplog, case):
    if case not in ("F2", "H1"): s.path.write_bytes(PAYLOAD)
    if case in ("H2", "H3", "H4", "H5"):
        exc = {"H2": PermissionError(errno.EACCES, "SYNTH_PASSKEY"),
               "H3": PermissionError(errno.EACCES, "SYNTH_PASSKEY"),
               "H4": FileNotFoundError(errno.ENOENT, "synthetic"), "H5": RuntimeError("synthetic")}[case]
        setattr(s, "lstat_effect" if case == "H3" else "unlink_effect", fail(exc))
    decoy = s.root / "decoy"; decoy.write_bytes(PAYLOAD)
    if case in ("J2", "J3"):
        def swap():
            info = s.real_lstat(s.path); s.real_unlink(s.path)
            if case == "J2": s.path.symlink_to(decoy)
            else: s.path.mkdir(); (s.path / "child").write_bytes(PAYLOAD)
            return info
        s.lstat_effect = swap
    if case == "H5":
        with pytest.raises(RuntimeError, match="synthetic"): s.daemon.start()
        return
    s.daemon.start()
    if case == "F2":
        s.daemon._proc = None; s.daemon.start()
        assert s.lstats == 2, "F2: each eligible start must attempt lstat"
    if case in ("H2", "H3", "J3"):
        number = errno.EISDIR if case == "J3" else errno.EACCES
        assert records(caplog) == [("WARNING", f"legacy_aria2_session_cleanup_failed: errno={number}")]
        if case == "J3": assert data(s.path / "child") == PAYLOAD, "J3: raced directory child changed"
        else: assert data(s.path) == PAYLOAD, f"{case}: failed cleanup changed session"
    else: assert records(caplog) == []
    if case == "J2": assert not os.path.lexists(s.path) and data(decoy) == PAYLOAD, "J2: entry remains or decoy changed"


@pytest.mark.parametrize("case", ["I1", "I2", "I3", "I4", "I6", "I7"], ids=str)
def test_I1_I2_I3_I4_I6_I7_lifecycle(s, monkeypatch, caplog, case):
    s.path.write_bytes(PAYLOAD)
    if case in ("I1", "I2", "I3"):
        s.connect_error = None
        if case == "I3": s.reclaim.side_effect = aria2.Aria2Error("synthetic foreign daemon")
        if case == "I1": s.reclaim.side_effect = lambda: setattr(s, "connect_error", ConnectionRefusedError())
    if case == "I3":
        with pytest.raises(aria2.Aria2Error, match="^synthetic foreign daemon$"): s.daemon.start()
        assert data(s.path) == PAYLOAD, "I3: reclaim failure removed the session"
        return
    if case == "I6": s.daemon._proc = s.popen.return_value; s.daemon._proc.poll.return_value = None
    if case == "I7":
        dead, live = MagicMock(), MagicMock(); dead.poll.return_value = 1; live.poll.return_value = None
        children = iter([dead, live])
        s.popen.side_effect = lambda *a, **k: s.events.append("spawn") or next(children)
        shutdown = MagicMock(); monkeypatch.setattr(aria2.Aria2RPC, "shutdown", shutdown)
        monkeypatch.setattr(s.daemon, "_wait_for_port_release", lambda: None)
    if case == "I7":
        with pytest.raises(aria2.Aria2Error, match="Another aria2 process"): s.daemon.start()
        assert s.events == ["port", "unlink", "spawn"] and s.popen.call_count == 1
        shutdown.assert_not_called()
        assert s.lstats == 1 and not s.path.exists()
        return
    s.daemon.start()
    if case in ("I1", "I2"):
        assert data(s.path) == PAYLOAD, f"{case}: busy-port start changed session"; s.reclaim.assert_called_once(); skipped(caplog, "rpc_port_busy")
    elif case == "I6": assert s.lstats == 0 and not s.popen.called
    else:
        assert s.events == ["port", "unlink", "spawn"], f"{case}: cleanup order/count"
        assert s.lstats == 1


@pytest.mark.parametrize("case", ["H2", "G1", "I1", "P6-timeout", "P6-eacces"], ids=["H2-warning", "G1-skip", "I1-busy", "P6-timeout", "P6-eacces"])
def test_H6_H7_log_hygiene_and_honesty(s, monkeypatch, caplog, capsys, case):
    s.path.write_bytes(PAYLOAD)
    exc = PermissionError(errno.EACCES, f"{s.path} {PAYLOAD!r}")
    if case == "H2": s.unlink_effect = fail(exc)
    if case == "G1":
        s.real_unlink(s.path); decoy = s.root / "decoy"; decoy.write_bytes(PAYLOAD); s.path.symlink_to(decoy)
    if case == "I1": s.connect_error = None
    if case.startswith("P6"): s.connect_error = TimeoutError(str(exc)) if case.endswith("timeout") else exc
    s.creates.clear(); s.daemon.start(); captured = capsys.readouterr()
    text = " ".join(r.getMessage() for r in caplog.records) + captured.out + captured.err
    assert not any(x in text for x in ("SYNTH_TOKEN", "SYNTH_PASSKEY", str(s.path), str(s.data), str(exc))), "H6: leaked material"
    assert not any(x in text.lower() for x in ("removed", "cleaned", "deleted")), "H7: false success claim"
    if case.startswith("P6"):
        assert data(s.path) == PAYLOAD and (s.lstats, s.opens, s.events.count("unlink"), s.creates) == (0, [], 0, []), "P6: inconclusive probe accessed session"
        skipped(caplog, "rpc_port_not_refused")


@pytest.mark.parametrize("case,error", [("P1", ConnectionRefusedError()), ("P2", TimeoutError()),
    ("P3-eacces", OSError(errno.EACCES, "synthetic")), ("P3-unreachable", OSError(errno.ENETUNREACH, "synthetic")), ("P3-reset", ConnectionResetError()), ("P3-aborted", ConnectionAbortedError()),
    ("P3-no-errno", OSError()), ("P4", None), ("P5-return", None), ("P5-raise", None), ("P8", None), ("P9-timeout", TimeoutError()), ("P9-eacces", OSError(errno.EACCES, "synthetic")), ("P9-unprobed", None)],
    ids=["P1", "P2", "P3-eacces", "P3-unreachable", "P3-reset", "P3-aborted", "P3-no-errno", "P4", "P5-return", "P5-raise", "P8", "P9-timeout", "P9-eacces", "P9-unprobed"])
def test_P1_P2_P3_P4_P5_P8_probe_cleanup(s, monkeypatch, caplog, case, error):
    if case.startswith("P9"):
        s.path.write_bytes(PAYLOAD); s.daemon.start()
        assert not s.path.exists(), "P9: refused first launch must remove session"
        s.daemon._proc = None; s.lstats = 0; s.events.clear(); s.opens.clear(); s.creates.clear(); caplog.clear(); s.popen.reset_mock(); s.reclaim.reset_mock()
    s.path.write_bytes(PAYLOAD); s.creates.clear(); s.connect_error = error
    if case in ("P8", "P9-unprobed"): monkeypatch.setattr(s.daemon, "_port_in_use", lambda: False)
    if case.startswith("P5"):
        def reclaim():
            s.connect_error = ConnectionRefusedError()
            assert aria2.Aria2Daemon._port_in_use(s.daemon) is False
            if case == "P5-raise": raise aria2.Aria2Error("synthetic reclaim failure")
        s.reclaim.side_effect = reclaim
    if case == "P5-raise":
        with pytest.raises(aria2.Aria2Error, match="^synthetic reclaim failure$"): s.daemon.start()
    else: s.daemon.start()
    if case == "P1":
        assert not s.path.exists(), "P1: explicit refusal must permit cleanup"; assert s.popen.called and not s.reclaim.called
        return
    assert data(s.path) == PAYLOAD, f"{case}: non-refused launch changed session"
    assert (s.lstats, s.opens, s.events.count("unlink"), s.creates) == (0, [], 0, []), f"{case}: skip accessed session"
    busy = case == "P4" or case.startswith("P5")
    if busy: s.reclaim.assert_called_once()
    else: s.reclaim.assert_not_called()
    if case == "P5-raise": assert not s.popen.called and records(caplog) == []
    else:
        assert s.popen.called, f"{case}: skip must preserve startup"
        skipped(caplog, "rpc_port_busy" if busy else "rpc_port_not_refused")

@pytest.mark.parametrize("error,expected", [(ConnectionRefusedError(), False), (TimeoutError(), False),
    (OSError(errno.EACCES, "synthetic"), False), (OSError(errno.ENETUNREACH, "synthetic"), False),
    (ConnectionResetError(), False), (ConnectionAbortedError(), False), (OSError(), False), (None, True)], ids=["P7-refused", "P7-timeout", "P7-eacces", "P7-unreachable", "P7-reset", "P7-aborted", "P7-no-errno", "P7-busy"])
def test_P7_port_probe_semantics(s, error, expected):
    s.connect_error = error; s.daemon._rpc_refused = True
    assert aria2.Aria2Daemon._port_in_use(s.daemon) is expected, "P7: existing port probe semantics changed"
    assert s.daemon._rpc_refused is isinstance(error, ConnectionRefusedError), "P7: only explicit refusal may set eligibility"

def test_M1_readme_exact_approved_text():
    row = r"| aria2 log | `~/.local/share/cove/aria2.log` | `%USERPROFILE%\.local\share\cove\aria2.log` |"
    note = "Earlier versions of Cove kept an `aria2.session` file in this folder. Cove no longer creates that file or uses it to restore downloads; recovery uses the queue database and aria2's separate `.aria2` control files. When Cove starts its aria2 engine, it attempts to remove a leftover `aria2.session` in its current data folder only when a local connection check on its RPC port is explicitly refused and the configured path is an ordinary regular file. This does not prove that no aria2 process is running. It skips removal after daemon reclamation, when the port is in use or the check is inconclusive (for example, a timeout), when the file is missing or is not an ordinary file, or when removal fails; a later eligible start can try again. Removing a file does not guarantee that its contents are physically erased. Copies may remain in backups, snapshots, synced folders, hard links, or unused disk space. The cleanup does not target other installations' separate data folders or stop older Cove versions, which may recreate the file. This change does not touch `aria2.log`, the queue database, or saved `.torrent` files, which can still contain download addresses."
    text = (Path(__file__).parents[1] / "README.md").read_text()
    assert text.splitlines().count(row) == 1, "M1: exact final R1 row required"
    assert text.splitlines().count(note) == 1, "M1: exact final R2 paragraph required"
    assert note + "\n\nPortable builds keep everything in a `cove-app-data` folder next to the" in text
    assert "aria2 session / log" not in text and "[OPTIONAL" not in text
    assert not any(p in (row + note).lower() for p in ("securely erased", "permanently erased", "permanently removed", "all copies", "no session data remains", "all urls", "all logins"))
