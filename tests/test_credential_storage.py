"""S04 filesystem boundaries and real aria2 proxy authentication.

All credentials are synthetic and all product paths are scratch paths. POSIX
mode tests make no assertion about Windows ACLs. The local proxy serves bytes
itself; it never forwards a request or contacts example.invalid.
"""
import base64
import http.server
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest

from cove import aria2, config, db
from cove.aria2 import Aria2Daemon, Aria2Error, Aria2RPC
from cove.config import Settings
from tests.test_aria2_external import ScriptedRPC


POSIX_MODES = pytest.mark.skipif(
    os.name == "nt", reason="POSIX modes do not describe Windows ACLs",
)
PAYLOAD = b"S04_SYNTHETIC_PAYLOAD_" + bytes(range(65, 91)) * 40
RPC_SECRET = "S04_RPC_SECRET_real_0123456789"


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    paths = SimpleNamespace(
        root=tmp_path, config=tmp_path / "xdg-config" / "cove",
        data=tmp_path / "xdg-data" / "cove", downloads=tmp_path / "downloads",
    )
    paths.settings = paths.config / "settings.json"
    paths.database = paths.data / "cove.db"
    paths.conf = paths.data / "aria2.rpc.auth"
    for name, value in (
        ("CONFIG_DIR", paths.config), ("CONFIG_FILE", paths.settings),
        ("DATA_DIR", paths.data), ("DB_FILE", paths.database),
        ("ARIA2_SESSION", paths.data / "aria2.session"),
        ("ARIA2_LOG", paths.data / "aria2.log"),
    ):
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(db, "DB_FILE", paths.database)
    monkeypatch.setattr(db, "DATA_DIR", paths.data, raising=False)
    monkeypatch.setattr(aria2, "DATA_DIR", paths.data)
    monkeypatch.setattr(aria2, "ARIA2_SESSION", paths.data / "aria2.session")
    monkeypatch.setattr(aria2, "ARIA2_LOG", paths.data / "aria2.log")
    return paths


def _settings(storage, **overrides):
    values = dict(
        download_dir=str(storage.downloads), rpc_secret=RPC_SECRET,
        proxy_type="http", proxy_host="127.0.0.1", proxy_port=18080,
        proxy_username="s04user", proxy_password="S04_ARGV_SECRET_9f3c",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def mock_start(storage, monkeypatch):
    proc = MagicMock()
    proc.poll.return_value = None
    def spawned(args, **kwargs):
        storage.launch = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--conf-path=")))
        storage.launch_text = storage.launch.read_text()
        return proc
    popen = MagicMock(side_effect=spawned)
    monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: "aria2c")
    monkeypatch.setattr(aria2, "_OwnedPopen", popen)
    monkeypatch.setattr(Aria2Daemon, "_port_in_use", lambda self: False)
    storage.wire = ScriptedRPC(monkeypatch, storage.data)
    daemons = []

    def make(**overrides):
        daemon = Aria2Daemon(_settings(storage, **overrides))
        daemons.append(daemon)
        return daemon, popen

    try:
        yield make
    finally:
        for daemon in daemons:
            daemon.stop()


@POSIX_MODES
@pytest.mark.parametrize("mask", [0o022, 0o000], ids=["umask022", "umask000"])
def test_s04_rollback_journal_stays_inside_private_boundary(storage, mask):
    """D5: observe the live sidecar, before commit can remove it."""
    previous = os.umask(mask)
    try:
        db.init(storage.database)
        with db.connect(storage.database) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO downloads (url,out_dir,created_at,cookies) VALUES (?,?,?,?)",
                ("http://example.invalid/s04.bin", str(storage.downloads), 1, "S04_SYNTHETIC_COOKIE"),
            )
            journal = Path(str(storage.database) + "-journal")
            assert journal.is_file()
            observed = {"journal": oct(_mode(journal)), "parent": oct(_mode(journal.parent))}
            assert journal.parent == storage.data
            assert _mode(storage.data) == 0o700, observed
            assert _mode(storage.database) == 0o600, observed
    finally:
        os.umask(previous)


@POSIX_MODES
@pytest.mark.parametrize("mask", [0o022, 0o000], ids=["umask022", "umask000"])
def test_s04_wal_sidecars_stay_inside_private_boundary(storage, mask):
    """D6: WAL is enabled in this isolated test only, never by production."""
    previous = os.umask(mask)
    try:
        db.init(storage.database)
        with db.connect(storage.database) as conn:
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
            conn.execute("INSERT INTO downloads (url,out_dir,created_at) VALUES (?,?,?)",
                         ("http://example.invalid/s04.bin", str(storage.downloads), 1))
            conn.commit()
            sidecars = [Path(str(storage.database) + suffix) for suffix in ("-wal", "-shm")]
            assert all(path.is_file() for path in sidecars)
            observed = {path.name: oct(_mode(path)) for path in sidecars}
            assert all(path.parent == storage.data for path in sidecars)
            assert _mode(storage.data) == 0o700, observed
            assert _mode(storage.database) == 0o600, observed
    finally:
        os.umask(previous)


@POSIX_MODES
@pytest.mark.parametrize("operation", ["init", "connect"])
def test_s04_db_symlink_fails_before_sqlite_without_touching_target(storage, monkeypatch, operation):
    """D7/M4: a valid unrelated database must never be opened through a link."""
    target = storage.root / "unrelated.db"
    db.init(target)
    with db.connect(target) as conn:
        conn.execute("INSERT INTO downloads (url,out_dir,created_at,cookies) VALUES (?,?,?,?)",
                     ("http://example.invalid/s04.bin", str(storage.root), 1, "S04_SYNTHETIC_COOKIE"))
    target.chmod(0o644)
    before_bytes, before_mode = target.read_bytes(), _mode(target)
    storage.data.mkdir(parents=True, mode=0o700)
    storage.database.symlink_to(target)
    opened = []
    original_connect = sqlite3.connect

    def observe_connect(path, *args, **kwargs):
        opened.append(path)
        return original_connect(path, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", observe_connect)
    error = None
    previous = os.umask(0o022)
    try:
        try:
            if operation == "init":
                db.init(storage.database)
            else:
                with db.connect(storage.database) as conn:
                    conn.execute("SELECT cookies FROM downloads").fetchone()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            error = exc
    finally:
        os.umask(previous)
    assert target.read_bytes() == before_bytes
    assert _mode(target) == before_mode
    assert not opened, "a symlinked DB must be refused before sqlite3.connect"
    assert error is not None and str(error), "symlink rejection needs a clear error"
    assert "S04_SYNTHETIC_COOKIE" not in str(error)
    assert storage.database.is_symlink()


_UNSAFE_LEAF_CALLERS = [
    pytest.param("config", "load", id="config-load"),
    pytest.param("config", "save", id="config-save"),
    *[pytest.param("data", operation, id=f"data-{operation}")
      for operation in ("load", "save", "init", "connect", "start")],
]


@POSIX_MODES
@pytest.mark.parametrize("leaf,operation", _UNSAFE_LEAF_CALLERS)
@pytest.mark.parametrize("kind", ["symlink", "regular-file"])
def test_s04_unsafe_owned_leaf_fails_without_touching_target(storage, mock_start, leaf, operation, kind):
    """D8: reject final symlink/non-directory leaves, never chmod their targets."""
    owned = getattr(storage, leaf)
    owned.parent.mkdir(parents=True)
    target = storage.root / "unrelated"
    if kind == "symlink":
        target.mkdir()
        target.chmod(0o755)
        (target / "sentinel").write_bytes(b"S04_SYNTHETIC_COOKIE")
        (target / "sentinel").chmod(0o644)
        owned.symlink_to(target, target_is_directory=True)
        before = {p.name: (p.read_bytes(), _mode(p)) for p in target.iterdir()}
    else:
        owned.write_bytes(b"S04_SYNTHETIC_COOKIE")
        owned.chmod(0o644)
        target = owned
        before = target.read_bytes()
    before_mode = _mode(target)
    daemon, popen = mock_start()
    error = None
    previous = os.umask(0o022)
    try:
        try:
            if operation == "load":
                Settings.load()
            elif operation == "save":
                _settings(storage).save()
            elif operation == "init":
                db.init(storage.database)
            elif operation == "connect":
                with db.connect(storage.database):
                    pass
            else:
                daemon.start()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            error = exc
    finally:
        os.umask(previous)
    after = ({p.name: (p.read_bytes(), _mode(p)) for p in target.iterdir()}
             if kind == "symlink" else target.read_bytes())
    assert after == before, "unrelated target contents must remain untouched"
    assert _mode(target) == before_mode
    assert not popen.called
    assert error is not None and str(error)
    assert "S04_SYNTHETIC_COOKIE" not in str(error)


@POSIX_MODES
def test_s04_user_download_directory_mode_unchanged(storage, mock_start):
    """D9/M5: a user's download directory is not a Cove-owned storage leaf."""
    storage.downloads.mkdir()
    storage.downloads.chmod(0o711)
    previous = os.umask(0o022)
    try:
        _settings(storage).save()
        loaded = Settings.load()
        assert loaded.download_dir == str(storage.downloads)
        db.init(storage.database)
        daemon, _ = mock_start(download_dir=loaded.download_dir)
        daemon.start()
    finally:
        os.umask(previous)
    assert _mode(storage.downloads) == 0o711


@POSIX_MODES
def test_s04_custom_db_parent_mode_unchanged(storage):
    parent = storage.root / "custom-parent"
    parent.mkdir()
    parent.chmod(0o755)
    previous = os.umask(0o000)
    try:
        db.init(parent / "custom.db")
        with db.connect(parent / "custom.db") as conn:
            assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0
    finally:
        os.umask(previous)
    assert _mode(parent) == 0o755
    assert not storage.data.exists(), "custom DB paths must not create a default DATA_DIR"


@POSIX_MODES
@pytest.mark.parametrize("mask", [0o022, 0o000], ids=["umask022", "umask000"])
@pytest.mark.parametrize("operation", ["init", "connect"])
def test_s04_custom_db_private_before_sqlite(storage, monkeypatch, mask, operation):
    """Custom paths get a private file without adopting their parent directory."""
    parent = storage.root / "custom-parent"
    parent.mkdir()
    parent.chmod(0o755)
    path = parent / "custom.db"
    original_connect = sqlite3.connect
    observed = []

    def observe_connect(database, *args, **kwargs):
        observed.append(_mode(path) if path.exists() else None)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", observe_connect)
    previous = os.umask(mask)
    try:
        if operation == "init":
            db.init(path)
        else:
            with db.connect(path) as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        os.umask(previous)
    assert _mode(parent) == 0o755
    assert observed == [0o600], "securely create the file before SQLite can create it"
    assert _mode(path) == 0o600


@POSIX_MODES
@pytest.mark.parametrize("check", ["private-leaf", "parents-unchanged"])
def test_s04_portable_storage_owns_only_final_leaf(tmp_path, check):
    """D10: exercise import-time portable paths in a fresh subprocess."""
    exe_root = tmp_path / "exe"
    parent = exe_root / "cove-app-data"
    parent.mkdir(parents=True)
    exe_root.chmod(0o775)
    parent.chmod(0o755)
    script = r'''
import json, os, stat, sys
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch
previous = os.umask(0o022)
try:
    sys.argv[0] = str(Path(sys.argv[1]) / "cove")
    from cove import config, db, aria2
    from tests.test_aria2_external import ScriptedRPC
    settings = config.Settings.load()
    settings.download_dir = str(Path(sys.argv[0]).parent / "downloads")
    settings.save()
    db.init()
    proc = MagicMock()
    proc.poll.return_value = None
    ScriptedRPC(SimpleNamespace(setattr=setattr), config.DATA_DIR)
    daemon = aria2.Aria2Daemon(settings)
    try:
        with patch.object(aria2, "_resolve_aria2c", return_value="aria2c"), \
             patch.object(aria2, "_OwnedPopen", return_value=proc), \
             patch.object(aria2.Aria2Daemon, "_port_in_use", return_value=False):
            daemon.start()
    finally:
        daemon.stop()
    mode = lambda p: stat.S_IMODE(Path(p).stat().st_mode)
    print(json.dumps({"config": str(config.CONFIG_DIR), "data": str(config.DATA_DIR),
                      "leaf": mode(config.DATA_DIR), "exe": mode(Path(sys.argv[0]).parent),
                      "parent": mode(config.DATA_DIR.parent), "db": mode(config.DB_FILE)}))
finally:
    os.umask(previous)
'''
    env = dict(os.environ, COVE_PORTABLE="1", XDG_CONFIG_HOME=str(tmp_path / "xdg-config"),
               XDG_DATA_HOME=str(tmp_path / "xdg-data"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-c", script, str(exe_root)],
                            cwd=Path(__file__).resolve().parents[1], env=env,
                            capture_output=True, text=True, timeout=10, check=True)
    modes = json.loads(result.stdout)
    assert modes["data"] == modes["config"] == str(parent / "cove-download-manager")
    if check == "private-leaf":
        assert (modes["leaf"], modes["db"]) == (0o700, 0o600)
    else:
        assert (modes["exe"], modes["parent"]) == (0o775, 0o755)


@POSIX_MODES
@pytest.mark.parametrize("mask", [0o022, 0o000], ids=["umask022", "umask000"])
def test_s04_conf_and_data_private_at_creation(storage, mock_start, monkeypatch, mask):
    """P3/M7: measure initial inode modes, before a later chmod can hide them."""
    daemon, popen = mock_start()
    directory_modes, file_modes = [], []
    original_mkdir, original_open, original_path_open = os.mkdir, os.open, Path.open
    original_replace = os.replace
    original_fsync = os.fsync
    synced_inodes = set()
    publications = []

    def conf_candidate(path, dir_fd=None):
        path = Path(path)
        if path.name in ("aria2.session", "aria2.log"):
            return False
        if dir_fd is not None and not path.is_absolute():
            parent = os.fstat(dir_fd)
            owned = storage.data.stat()
            return (parent.st_dev, parent.st_ino) == (owned.st_dev, owned.st_ino)
        return path.parent == storage.data

    def observe_mkdir(path, mode=0o777, *, dir_fd=None):
        result = original_mkdir(path, mode, dir_fd=dir_fd)
        made = os.stat(path, dir_fd=dir_fd)
        if storage.data.exists() and (made.st_dev, made.st_ino) == (
            storage.data.stat().st_dev, storage.data.stat().st_ino,
        ):
            directory_modes.append(stat.S_IMODE(made.st_mode))
        return result

    def observe_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if flags & os.O_CREAT and conf_candidate(path, dir_fd):
            file_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd

    def observe_path_open(path, *args, **kwargs):
        missing = not path.exists()
        handle = original_path_open(path, *args, **kwargs)
        if missing and conf_candidate(path):
            file_modes.append(stat.S_IMODE(os.fstat(handle.fileno()).st_mode))
        return handle

    def observe_replace(source, destination, *args, **kwargs):
        if Path(destination) == storage.conf:
            assert Path(source) != storage.conf
            assert not Path(source).is_symlink()
            assert Path(source).stat().st_ino in synced_inodes, "fsync the conf before publication"
            publications.append((_mode(Path(source)), Path(source).read_text()))
        return original_replace(source, destination, *args, **kwargs)

    def observe_fsync(fd):
        result = original_fsync(fd)
        synced_inodes.add(os.fstat(fd).st_ino)
        return result

    monkeypatch.setattr(os, "mkdir", observe_mkdir)
    monkeypatch.setattr(os, "open", observe_open)
    monkeypatch.setattr(Path, "open", observe_path_open)
    monkeypatch.setattr(os, "replace", observe_replace)
    monkeypatch.setattr(os, "fsync", observe_fsync)
    previous = os.umask(mask)
    try:
        daemon.start()
    finally:
        os.umask(previous)
    assert popen.called
    assert storage.conf.is_file() and _mode(storage.conf) == 0o600
    assert file_modes and all(mode == 0o600 for mode in file_modes), file_modes
    assert directory_modes == [0o700], "DATA_DIR must be private when first created"
    assert _mode(storage.data) == 0o700
    assert publications == [(0o600, storage.conf.read_text())], "publish the completed conf atomically"


@POSIX_MODES
def test_s04_hostile_conf_symlink_is_replaced_without_touching_target(storage, mock_start):
    """P9/M11: os.replace must replace the link, never follow it."""
    storage.data.mkdir(parents=True, mode=0o700)
    target = storage.root / "unrelated.conf"
    target.write_bytes(b"S04_OLD_PROXY_SECRET\nunrelated bytes\n")
    target.chmod(0o644)
    before = target.read_bytes(), _mode(target)
    storage.conf.symlink_to(target)
    daemon, _ = mock_start()
    previous = os.umask(0o022)
    try:
        daemon.start()
    finally:
        os.umask(previous)
    assert (target.read_bytes(), _mode(target)) == before
    assert not storage.conf.is_symlink()
    assert storage.conf.is_file() and _mode(storage.conf) == 0o600
    assert storage.conf.read_text().splitlines() == [
        storage.launch_text.splitlines()[0],
    ]


@POSIX_MODES
@pytest.mark.parametrize("failure", ["spawn", "rpc"])
def test_s04_startup_failure_is_private_and_does_not_echo_secrets(storage, mock_start, monkeypatch, failure):
    """P10: synthetic error text must not echo a credential into the UI."""
    daemon, popen = mock_start()
    secrets = (RPC_SECRET, "S04_ARGV_SECRET_9f3c", "s04user")
    message = "synthetic failure " + " ".join(secrets)
    if failure == "spawn":
        popen.side_effect = OSError(message)
    else:
        import requests
        storage.wire.failure = requests.ConnectionError(message)
        await_rpc = daemon._await_rpc
        monkeypatch.setattr(daemon, "_await_rpc", lambda: await_rpc(timeout=0.01))
    previous = os.umask(0o000)
    try:
        with pytest.raises(Aria2Error) as raised:
            daemon.start()
    finally:
        os.umask(previous)
    assert all(secret not in str(raised.value) for secret in secrets)
    assert _mode(storage.data) == 0o700
    assert storage.conf.is_file() and _mode(storage.conf) == 0o600
    assert all(secret not in "\0".join(popen.call_args.args[0]) for secret in secrets)
    assert all(_mode(path) == 0o600 for path in storage.data.iterdir()
               if path.name not in ("aria2.session", "aria2.log"))


class _AuthProxy:
    """Basic-auth fixture copied from the approved scratch proof, no forwarding."""

    def __init__(self, username, password):
        self.username, self.password = username, password
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                header = self.headers.get("Proxy-Authorization", "")
                user = password = None
                if header.lower().startswith("basic "):
                    try:
                        value = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
                        user, _, password = value.partition(":")
                    except (ValueError, UnicodeError):
                        pass
                accepted = (user, password) == (outer.username, outer.password)
                outer.requests.append((self.path, user, password, accepted))
                body = PAYLOAD if accepted else b"proxy auth required"
                self.send_response(200 if accepted else 407)
                if not accepted:
                    self.send_header("Proxy-Authenticate", 'Basic realm="s04"')
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True,
        )
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        assert not self.thread.is_alive()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def real_proxy(storage, monkeypatch):
    executable = shutil.which("aria2c")
    if executable is None:
        pytest.skip("real aria2c is not installed")
    # Leave all_proxy/ALL_PROXY intact so the C2 regression cannot be masked.
    for key in ("http_proxy", "https_proxy", "ftp_proxy", "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(aria2, "_resolve_aria2c", lambda: executable)
    monkeypatch.setattr(Aria2Daemon, "_port_in_use", lambda self: False)
    original_session = Aria2RPC._session

    def local_rpc_session(self):
        session = original_session(self)
        session.trust_env = False  # Test proxy env routing in aria2, not in the loopback RPC client.
        return session

    monkeypatch.setattr(Aria2RPC, "_session", local_rpc_session)
    original_popen = subprocess.Popen
    launched = []
    legacy_proxy_url = None

    def isolated_popen(args, **kwargs):
        storage.launch = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--conf-path=")))
        storage.launch_text = storage.launch.read_text()
        # Disable swarm discovery and cap network waits in this HTTP-only test.
        args = [*args, "--enable-dht=false", "--enable-dht6=false", "--enable-peer-exchange=false",
                "--bt-enable-lpd=false", "--disable-ipv6=true", "--max-tries=1",
                "--retry-wait=0", "--connect-timeout=1", "--timeout=1"]
        if legacy_proxy_url is not None:
            # Synthetic-only empirical oracle for the pre-S04 argv precedence.
            args.append(f"--all-proxy={legacy_proxy_url}")
            kwargs["env"] = dict(os.environ)
        proc = original_popen(args, **kwargs)
        launched.append(proc)
        return proc

    monkeypatch.setattr(aria2, "_OwnedPopen", isolated_popen)
    resources = []

    def make(username="s04user", password="S04_ARGV_SECRET_9f3c", *, proxy_argv=False):
        nonlocal legacy_proxy_url
        try:
            proxy = _AuthProxy(username, password)
        except PermissionError:
            pytest.skip("loopback sockets are unavailable in this execution environment")
        try:
            settings = _settings(storage, rpc_port=_free_port(), proxy_port=proxy.port,
                                 proxy_username=username, proxy_password=password)
            daemon = Aria2Daemon(settings)
            rpc = Aria2RPC(settings, timeout=0.5, secret_provider=daemon.active_rpc_secret)
        except BaseException:
            proxy.stop()
            raise
        resources.append((daemon, rpc, proxy))
        legacy_proxy_url = daemon._build_proxy_url() if proxy_argv else None
        try:
            daemon.start()
        finally:
            legacy_proxy_url = None
        return daemon, rpc, proxy

    try:
        yield make
    finally:
        try:
            for daemon, rpc, proxy in resources:
                try:
                    rpc.close()
                    daemon.stop()
                finally:
                    proxy.stop()
        finally:
            # Reap only children this fixture created, including failed starts.
            for proc in launched:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=2)


def _download_status(rpc, storage, filename):
    gid = rpc.add_uri(["http://example.invalid/s04.bin"], str(storage.downloads), 1, filename=filename)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = rpc.tell_status(gid)
        if status["status"] in ("complete", "error", "removed"):
            break
        time.sleep(0.02)
    else:
        pytest.fail("local proxy download exceeded its two-second deadline")
    return status


def _download(rpc, storage, filename="s04.bin"):
    status = _download_status(rpc, storage, filename)
    assert status["status"] == "complete", status
    assert (storage.downloads / filename).read_bytes() == PAYLOAD


@pytest.mark.parametrize("key", [
    "all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
    "ftp_proxy", "FTP_PROXY", "no_proxy", "NO_PROXY",
])
@pytest.mark.parametrize("empty", [False, True], ids=["conflicting", "empty"])
def test_s04_real_proxy_environment_matches_argv_precedence(storage, real_proxy, monkeypatch, key, empty):
    """C2: empirical 1.37.0 oracle for exactly which env names changed priority.

    POSIX: only all_proxy needs stripping. Uppercase names are ignored;
    http_proxy/https_proxy/ftp_proxy and no_proxy keep their pre-S04 priority.
    Windows getenv treats environment names case-insensitively.
    """
    value = "" if empty else ("example.invalid" if key.lower() == "no_proxy" else "http://127.0.0.1:1")
    monkeypatch.setenv(key, value)
    previous = dict(os.environ)
    baseline_daemon, baseline_rpc, baseline_proxy = real_proxy(proxy_argv=True)
    baseline = _download_status(baseline_rpc, storage, "argv.bin")
    baseline_options = baseline_rpc._call("aria2.getGlobalOption")
    baseline_daemon.stop()
    daemon, rpc, proxy = real_proxy()
    current = _download_status(rpc, storage, "conf.bin")
    current_options = rpc._call("aria2.getGlobalOption")
    option_names = ("all-proxy", "http-proxy", "https-proxy", "ftp-proxy", "no-proxy")
    expected_options = {name: baseline_options.get(name) for name in option_names}
    for name, option in expected_options.items():
        if isinstance(option, str):
            expected_options[name] = option.replace(
                f"127.0.0.1:{baseline_proxy.port}", f"127.0.0.1:{proxy.port}",
            )
    assert {name: current_options.get(name) for name in option_names} == expected_options
    assert current["status"] == baseline["status"]
    assert bool(proxy.requests) == bool(baseline_proxy.requests)
    if current["status"] == "complete":
        assert (storage.downloads / "conf.bin").read_bytes() == PAYLOAD
        assert (storage.downloads / "argv.bin").read_bytes() == PAYLOAD
        assert any(record[1:] == ("s04user", "S04_ARGV_SECRET_9f3c", True) for record in proxy.requests)
    if key.lower() == "all_proxy":
        assert current["status"] == "complete"
        assert any(record[1:] == ("s04user", "S04_ARGV_SECRET_9f3c", True) for record in proxy.requests)
    assert dict(os.environ) == previous


def test_s04_real_proxy_authentication_preserved(storage, real_proxy):
    """A GREEN baseline control: real start() still authenticates and downloads."""
    _, rpc, proxy = real_proxy()
    _download(rpc, storage)
    assert any(record == ("http://example.invalid/s04.bin", "s04user", "S04_ARGV_SECRET_9f3c", True)
               for record in proxy.requests)


def test_s04_real_proxy_secrets_absent_from_process_cmdline(storage, real_proxy):
    """P4: both protected transport and functioning authentication, real child."""
    daemon, rpc, proxy = real_proxy(password="S04_ARGV_SECRET_9f3c:@% #")
    _download(rpc, storage)
    assert any(record[-1] for record in proxy.requests)
    if sys.platform.startswith("linux"):
        cmdline = Path(f"/proc/{daemon._proc.pid}/cmdline").read_bytes()
        for secret in (RPC_SECRET, daemon.active_rpc_secret(), daemon.settings.proxy_username, daemon.settings.proxy_password,
                       quote(daemon.settings.proxy_password, safe="")):
            assert secret.encode() not in cmdline
        assert b"--all-proxy=" not in cmdline
    assert storage.conf.read_text().splitlines() == [
        f"rpc-secret={daemon.active_rpc_secret()}",
    ]
    assert storage.launch_text.splitlines() == [storage.conf.read_text().strip(), f"all-proxy={daemon._build_proxy_url()}"]
    assert not storage.launch.exists()


_SPECIAL_CREDENTIALS = [
    *[pytest.param("s04user", "S04_ARGV_SECRET_9f3c" + value, id="password-" + name)
      for name, value in (("colon", ":"), ("at", "@"), ("percent", "%"),
                          ("space", " "), ("hash", "#"), ("unicode", "雪"))],
    pytest.param("s04user", " S04_ARGV_SECRET_9f3c", id="password-leading-space"),
    pytest.param("s04user", " S04_ARGV_SECRET_9f3c ", id="password-surrounding-space"),
    *[pytest.param("s04user" + value, "S04_ARGV_SECRET_9f3c", id="username-" + name)
      for name, value in (("at", "@"), ("percent", "%"), ("space", " "),
                          ("hash", "#"), ("unicode", "雪"))],
    pytest.param(" s04user ", "S04_ARGV_SECRET_9f3c", id="username-surrounding-space"),
    pytest.param("s04user\r\n\x00\x01", "S04_ARGV_SECRET_9f3c\r\n\x00\x01", id="encoded-controls"),
]


@pytest.mark.parametrize("username,password", _SPECIAL_CREDENTIALS)
def test_s04_special_credential_url_semantics_preserved(storage, username, password):
    """GREEN control for the pre-existing percent-encoding semantics."""
    daemon = Aria2Daemon(_settings(storage, proxy_username=username, proxy_password=password))
    expected = f"http://{quote(username, safe='')}:{quote(password, safe='')}@127.0.0.1:18080"
    assert daemon._build_proxy_url() == expected


@pytest.mark.parametrize("username,password", _SPECIAL_CREDENTIALS)
def test_s04_special_credentials_conf_is_one_encoded_line(storage, mock_start, username, password):
    """P7/P8 without sockets: controls in userinfo become percent escapes."""
    daemon, popen = mock_start(proxy_username=username, proxy_password=password)
    daemon.start()
    expected = f"http://{quote(username, safe='')}:{quote(password, safe='')}@127.0.0.1:18080"
    assert storage.conf.is_file() and storage.launch_text.splitlines() == [storage.conf.read_text().strip(), f"all-proxy={expected}"]
    assert not any(arg.startswith("--all-proxy") for arg in popen.call_args.args[0])


@pytest.mark.parametrize("username,password", _SPECIAL_CREDENTIALS)
def test_s04_special_credentials_round_trip_via_conf(storage, real_proxy, username, password):
    """P7/M9: percent-encoding must preserve exact Basic-auth credentials."""
    daemon, rpc, proxy = real_proxy(username, password)
    _download(rpc, storage)
    assert any(record[1:] == (username, password, True) for record in proxy.requests)
    expected = f"http://{quote(username, safe='')}:{quote(password, safe='')}@127.0.0.1:{proxy.port}"
    assert storage.launch_text.splitlines() == [storage.conf.read_text().strip(), f"all-proxy={expected}"]
    assert "all-proxy-user=" not in storage.launch_text
    assert "all-proxy-passwd=" not in storage.launch_text


def test_s04_rotated_password_used_by_real_proxy(storage, real_proxy):
    """P6: B must actually authenticate after restarting with a rotated conf."""
    daemon, rpc, proxy = real_proxy(password="S04_OLD_PROXY_SECRET")
    _download(rpc, storage, filename="old.bin")
    first = storage.launch_text
    daemon.stop()
    proxy.password = daemon.settings.proxy_password = "S04_ARGV_SECRET_9f3c_rotated"
    daemon.start()
    _download(rpc, storage, filename="rotated.bin")
    assert any(record[1:] == ("s04user", "S04_ARGV_SECRET_9f3c_rotated", True) for record in proxy.requests)
    assert "S04_OLD_PROXY_SECRET" not in storage.launch_text
    assert "S04_ARGV_SECRET_9f3c_rotated" in storage.launch_text
    assert "S04_OLD_PROXY_SECRET" in first
