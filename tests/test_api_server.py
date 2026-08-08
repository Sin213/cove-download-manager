import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from urllib import error as urlerror
from urllib import request as urlrequest

import pytest
from PySide6.QtCore import QCoreApplication, QObject, QThread

from cove.api_server import (
    MAX_BODY_BYTES,
    ApiProblem,
    LocalApiServer,
    QueueApiBridge,
    task_snapshot,
    validate_add_payload,
    validate_filename,
)
from cove import config
from cove import db
from cove import output_paths
from cove.config import Settings
from cove.output_paths import OutputPathError, validate_public_filename
from cove.queue import QueueManager
from cove.queue import DownloadTask


TOKEN = "t" * 43


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.download = {
            "task_id": 7,
            "gid": None,
            "url": "https://example.com/file.bin",
            "filename": None,
            "directory": "C:\\Downloads",
            "status": "queued",
            "backend": "aria2",
            "connections": 16,
            "speed_limit_kbps": 0,
            "completed_bytes": 0,
            "total_bytes": 0,
            "speed_bytes_per_second": 0,
            "progress_percent": 0.0,
            "error": None,
            "created_at": 1.0,
            "finished_at": None,
        }

    def invoke(self, action, payload=None):
        self.calls.append((action, payload or {}))
        if action == "settings":
            return {"download_directory": "C:\\Downloads", "api_port": 17681}
        if action == "list":
            return [self.download]
        if action in {"add", "status", "pause", "resume", "cancel"}:
            result = dict(self.download)
            if action == "cancel":
                result["status"] = "removed"
            return result
        raise AssertionError(action)


@pytest.fixture
def api_server():
    settings = SimpleNamespace(api_token=TOKEN, api_port=0)
    bridge = FakeBridge()
    server = LocalApiServer(settings, SimpleNamespace(), port=0, bridge=bridge)
    server.start()
    try:
        yield server, bridge
    finally:
        server.stop()


def call(server, method, path, *, body=None, headers=None):
    data = body
    request_headers = dict(headers or {})
    if isinstance(body, dict):
        data = json.dumps(body).encode()
        request_headers.setdefault("Content-Type", "application/json")
    req = urlrequest.Request(
        f"http://127.0.0.1:{server.bound_port}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urlrequest.urlopen(req, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urlerror.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_health_is_minimal_and_unauthenticated(api_server):
    server, _ = api_server
    status, payload = call(server, "GET", "/api/v1/health")
    assert status == 200
    assert payload["ok"] is True
    assert "token" not in json.dumps(payload).lower()


@pytest.mark.parametrize(
    ("header", "code"),
    [
        (None, "missing_auth"),
        ("Basic abc", "malformed_auth"),
        ("Bearer wrong", "invalid_token"),
    ],
)
def test_operational_endpoints_require_bearer_token(api_server, header, code):
    server, _ = api_server
    headers = {} if header is None else {"Authorization": header}
    status, payload = call(server, "GET", "/api/v1/downloads", headers=headers)
    assert status == 401
    assert payload["error"]["code"] == code


def test_rejects_browser_origin_even_with_valid_token(api_server):
    server, _ = api_server
    status, payload = call(
        server,
        "GET",
        "/api/v1/downloads",
        headers={**auth(), "Origin": "https://hostile.example"},
    )
    assert status == 403
    assert payload["error"]["code"] == "origin_not_allowed"


def test_rejects_wrong_content_type_malformed_and_oversized_json(api_server):
    server, _ = api_server
    status, payload = call(server, "POST", "/api/v1/downloads", body=b"{}", headers=auth())
    assert (status, payload["error"]["code"]) == (415, "unsupported_content_type")

    status, payload = call(
        server,
        "POST",
        "/api/v1/downloads",
        body=b"{bad",
        headers={**auth(), "Content-Type": "application/json"},
    )
    assert (status, payload["error"]["code"]) == (400, "malformed_json")

    status, payload = call(
        server,
        "POST",
        "/api/v1/downloads",
        body=b"x" * (MAX_BODY_BYTES + 1),
        headers={**auth(), "Content-Type": "application/json"},
    )
    assert (status, payload["error"]["code"]) == (413, "body_too_large")


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "invalid_url"),
        ({"url": "file:///C:/secret.txt"}, "invalid_url"),
        ({"url": "https://example.com/a", "filename": "../a"}, "invalid_filename"),
        ({"url": "https://example.com/a", "directory": "relative"}, "directory_not_absolute"),
        ({"url": "https://example.com/a", "connections": 0}, "invalid_connections"),
        ({"url": "https://example.com/a", "connections": 17}, "invalid_connections"),
        ({"url": "https://example.com/a", "connections": True}, "invalid_connections"),
        ({"url": "https://example.com/a", "create_directory": True}, "invalid_create_directory"),
        ({"url": "https://example.com/a", "surprise": 1}, "unknown_fields"),
    ],
)
def test_add_validation(payload, code):
    with pytest.raises(ApiProblem) as caught:
        validate_add_payload(payload)
    assert caught.value.code == code


@pytest.mark.parametrize("filename", ["report.txt", "notes-2026.txt"])
def test_api_filename_accepts_ordinary_ascii(filename):
    assert validate_filename(filename) == filename


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "/absolute.txt", "../escape.txt", "nested/file.txt", r"nested\file.txt"],
)
def test_api_filename_rejects_invalid_components(filename):
    with pytest.raises(ApiProblem) as caught:
        validate_filename(filename)
    assert caught.value.code == "invalid_filename"


@pytest.mark.parametrize(
    ("windows", "accepted", "rejected"),
    [
        (False, "é" * 125 + "a.txt", "é" * 126 + ".txt"),
        (True, "😀" * 125 + "a.txt", "😀" * 126 + ".txt"),
    ],
    ids=["posix-encoded-bytes", "windows-utf16-units"],
)
def test_api_filename_uses_platform_component_length(monkeypatch, windows, accepted, rejected):
    monkeypatch.setattr(output_paths, "_is_windows_runtime", lambda: windows)

    assert validate_filename(accepted) == accepted
    measured_length = len(accepted.encode("utf-16-le")) // 2 if windows else len(os.fsencode(accepted))
    assert measured_length == 255
    assert len(rejected) < 255
    with pytest.raises(ApiProblem) as caught:
        validate_filename(rejected)
    assert caught.value.code == "invalid_filename"


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "windows"])
@pytest.mark.parametrize(
    "filename",
    [
        "report.txt",
        "世界.txt",
        "😀.txt",
        "a" * 255,
        "é" * 127 + "a",
        "😀" * 127 + "a",
        "",
        ".",
        "..",
        "/absolute.txt",
        "../escape.txt",
        "nested/file.txt",
        r"nested\file.txt",
        "CON.txt",
        "trailing.",
    ],
)
def test_api_and_publication_filename_validity_match(monkeypatch, windows, filename):
    monkeypatch.setattr(output_paths, "_is_windows_runtime", lambda: windows)

    try:
        validate_filename(filename)
        api_valid = True
    except ApiProblem:
        api_valid = False
    try:
        validate_public_filename(filename)
        publication_valid = True
    except OutputPathError:
        publication_valid = False

    assert api_valid is publication_valid


def test_rejected_api_filename_is_not_queued(api_server, monkeypatch):
    monkeypatch.setattr(output_paths, "_is_windows_runtime", lambda: False)
    server, bridge = api_server

    status, payload = call(
        server,
        "POST",
        "/api/v1/downloads",
        body={"url": "https://example.com/file.bin", "filename": "é" * 126 + ".txt"},
        headers=auth(),
    )

    assert (status, payload["error"]["code"]) == (400, "invalid_filename")
    assert bridge.calls == []


def test_add_keeps_omitted_defaults_for_queue_thread(api_server):
    server, bridge = api_server
    status, payload = call(
        server,
        "POST",
        "/api/v1/downloads",
        body={"url": "https://example.com/file.bin"},
        headers=auth(),
    )
    assert status == 202
    assert payload["download"]["task_id"] == 7
    assert payload["download"]["gid"] is None
    action, passed = bridge.calls[-1]
    assert action == "add"
    assert passed["directory"] is None
    assert passed["connections"] is None


def test_list_status_pause_resume_and_safe_cancel(api_server):
    server, bridge = api_server
    status, listing = call(server, "GET", "/api/v1/downloads", headers=auth())
    assert status == 200 and listing["count"] == 1
    status, detail = call(server, "GET", "/api/v1/downloads/7", headers=auth())
    assert status == 200 and detail["download"]["task_id"] == 7
    for action in ("pause", "resume", "cancel"):
        status, result = call(
            server,
            "POST",
            f"/api/v1/downloads/7/{action}",
            body={},
            headers=auth(),
        )
        assert status == 200
        assert result["download"]["task_id"] == 7
    assert bridge.calls[-1] == ("cancel", {"task_id": 7})


def test_invalid_and_missing_task_paths_are_structured(api_server):
    server, _ = api_server
    status, payload = call(server, "GET", "/api/v1/downloads/0", headers=auth())
    assert (status, payload["error"]["code"]) == (400, "invalid_task_id")


def test_port_collision_and_clean_shutdown():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    settings = SimpleNamespace(api_token=TOKEN, api_port=port)
    server = LocalApiServer(settings, SimpleNamespace(), bridge=FakeBridge())
    with pytest.raises(OSError):
        server.start()
    listener.close()

    server = LocalApiServer(settings, SimpleNamespace(), bridge=FakeBridge())
    server.start()
    server.stop()
    replacement = socket.socket()
    replacement.bind(("127.0.0.1", port))
    replacement.close()


def test_qt_bridge_executes_add_on_queue_owning_thread():
    app = QCoreApplication.instance() or QCoreApplication([])

    class FakeQueue(QObject):
        def __init__(self):
            super().__init__()
            self.settings = SimpleNamespace()
            self.tasks = {}
            self.called_thread = None

        def add_url(self, url, **kwargs):
            self.called_thread = QThread.currentThread()
            self.tasks[1] = DownloadTask(id=1, url=url, out_dir="C:\\Downloads")
            return 1

    queue = FakeQueue()
    bridge = QueueApiBridge(queue, timeout=2)
    result = {}

    def worker():
        result["download"] = bridge.invoke(
            "add",
            {
                "url": "https://example.com/a",
                "directory": None,
                "filename": None,
                "connections": None,
                "speed_limit_kbps": 0,
            },
        )

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 2
    while thread.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    thread.join(timeout=0.1)
    assert not thread.is_alive()
    assert queue.called_thread is queue.thread()
    assert result["download"]["task_id"] == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/stream/master.m3u8",
        "https://www.youtube.com/watch?v=abc123",
    ],
)
@pytest.mark.parametrize(
    "overrides",
    [
        {"connections": 4},
        {"speed_limit_kbps": 500},
        {"speed_limit_kbps": 0},  # explicit zero is still an explicit option
    ],
)
def test_add_rejects_explicit_per_download_limits_for_video_backends(url, overrides):
    with pytest.raises(ApiProblem) as caught:
        validate_add_payload({"url": url, **overrides})
    assert caught.value.code == "unsupported_for_backend"
    assert caught.value.status == 400


def test_add_allows_video_urls_without_explicit_limits():
    validated = validate_add_payload({"url": "https://cdn.example.com/stream/master.m3u8"})
    assert validated["connections"] is None
    assert validated["speed_limit_kbps"] == 0


def test_bridge_cancel_never_deletes_and_hides_inflight_task_immediately():
    QCoreApplication.instance() or QCoreApplication([])

    class FakeQueue(QObject):
        def __init__(self):
            super().__init__()
            self.settings = SimpleNamespace()
            self.tasks = {
                9: DownloadTask(
                    id=9,
                    url="https://example.com/a",
                    out_dir="C:\\Downloads",
                    status="active",
                )
            }
            self.remove_call = None

        def remove(self, task_id, delete_file=False, keep_incomplete=False):
            self.remove_call = (task_id, delete_file, keep_incomplete)
            # QueueManager deliberately retains an add-in-flight task until
            # its gid arrives; the API must still hide it immediately.

    queue = FakeQueue()
    bridge = QueueApiBridge(queue)
    removed = bridge.invoke("cancel", {"task_id": 9})
    assert removed["status"] == "removed"
    # keep_incomplete pins the API contract: cancel never touches disk, even
    # for an unfinished aria2 download that the GUI's own Remove would clean.
    assert queue.remove_call == (9, False, True)
    assert bridge.invoke("list") == []
    with pytest.raises(ApiProblem) as caught:
        bridge.invoke("status", {"task_id": 9})
    assert caught.value.code == "task_not_found"


def test_http_add_enters_real_queue_sqlite_and_ui_signal_immediately(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    database = tmp_path / "cove.db"
    original_init = db.init
    original_connect = db.connect
    monkeypatch.setattr(db, "init", lambda: original_init(database))
    monkeypatch.setattr(db, "connect", lambda: original_connect(database))
    settings = Settings(
        download_dir=str(tmp_path),
        connections_per_server=16,
        api_token=TOKEN,
        api_port=0,
    )
    queue = QueueManager(settings, SimpleNamespace())
    queue._scheduler_allows = False
    added = []
    queue.task_added.connect(added.append)
    server = LocalApiServer(settings, queue, port=0)
    server.start()
    outcome = {}

    def worker():
        outcome["response"] = call(
            server,
            "POST",
            "/api/v1/downloads",
            body={
                "url": "https://example.com/model.gguf",
                "directory": str(tmp_path),
                "filename": "renamed.gguf",
                "connections": 8,
            },
            headers=auth(),
        )

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 3
    while thread.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    thread.join(timeout=0.1)
    try:
        assert not thread.is_alive()
        status, payload = outcome["response"]
        assert status == 202
        task_id = payload["download"]["task_id"]
        assert added == [task_id]
        task = queue.tasks[task_id]
        assert task.status == "queued"
        assert task.filename == "renamed.gguf"
        assert task.connections == 8
        with original_connect(database) as connection:
            row = connection.execute("SELECT * FROM downloads WHERE id=?", (task_id,)).fetchone()
        assert row["status"] == "queued"
        assert row["filename"] == "renamed.gguf"
        assert row["connections"] == 8
    finally:
        server.stop()
        queue._poll.stop()
        queue._ext_poll.stop()


def test_settings_migration_creates_distinct_api_token_without_changing_rpc_secret(
    tmp_path, monkeypatch
):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    settings_path = config_dir / "settings.json"
    config_dir.mkdir()
    rpc_secret = "r" * 32
    settings_path.write_text(json.dumps({"rpc_secret": rpc_secret, "rpc_port": 6800}))
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", settings_path)

    settings = config.Settings.load()

    assert settings.rpc_secret == rpc_secret
    assert len(settings.api_token) >= 24
    assert settings.api_token != rpc_secret
    persisted = json.loads(settings_path.read_text())
    assert persisted["rpc_secret"] == rpc_secret
    assert persisted["api_token"] == settings.api_token
    assert persisted["speed_limit_unit"] == "KB/s"


def test_settings_migration_repairs_invalid_api_types(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    settings_path = config_dir / "settings.json"
    config_dir.mkdir()
    settings_path.write_text(
        json.dumps(
            {
                "rpc_secret": "r" * 32,
                "api_token": "t" * 43,
                "api_port": True,
                "api_enabled": "yes",
                "speed_limit_unit": "GB/s",
                "connections_per_server": 32,
            }
        )
    )
    monkeypatch.setattr(config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "CONFIG_FILE", settings_path)

    settings = config.Settings.load()

    assert settings.api_port == config.DEFAULT_API_PORT
    assert settings.api_enabled is True
    assert settings.speed_limit_unit == "KB/s"
    assert settings.connections_per_server == config.MAX_CONNECTIONS_PER_SERVER
    assert max(config.CONNECTION_CHOICES) == config.MAX_CONNECTIONS_PER_SERVER
    persisted = json.loads(settings_path.read_text())
    assert persisted["api_port"] == config.DEFAULT_API_PORT
    assert persisted["api_enabled"] is True
    assert persisted["speed_limit_unit"] == "KB/s"
    assert persisted["connections_per_server"] == config.MAX_CONNECTIONS_PER_SERVER


def test_windows_packaging_explicitly_includes_api_server():
    root = Path(__file__).resolve().parents[1]
    wine_script = root / "scripts" / "build-windows-wine.sh"
    workflow = root / ".github" / "workflows" / "release.yml"
    assert "--hidden-import cove.api_server" in wine_script.read_text(encoding="utf-8")
    workflow_text = workflow.read_text(encoding="utf-8")
    assert workflow_text.count("--hidden-import cove.api_server") == 2


# ---------------------------------------------------------------------------
# TorBox regression: settings/task serialization boundaries are unchanged
# ---------------------------------------------------------------------------


def test_settings_endpoint_omits_every_debrid_credential_including_torbox(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    settings = Settings(
        download_dir=str(tmp_path),
        api_token=TOKEN,
        api_port=0,
        all_debrid_enabled=True,
        all_debrid_api_key="ad-key-value",
        real_debrid_enabled=True,
        real_debrid_api_token="rd-token-value",
        torbox_enabled=True,
        torbox_api_token="torbox-token-value",
        debrid_preferred_provider="torbox",
    )
    queue = QueueManager(settings, SimpleNamespace())
    try:
        bridge = QueueApiBridge(queue)
        result = bridge._handle("settings", {})
        for forbidden in (
            "torbox_enabled", "torbox_api_token", "debrid_preferred_provider",
            "real_debrid_api_token", "all_debrid_api_key",
        ):
            assert forbidden not in result
    finally:
        queue._poll.stop()
        queue._ext_poll.stop()


def test_task_snapshot_omits_internal_torbox_identifiers():
    task = DownloadTask(
        id=1,
        url="https://rapidgator.net/f",
        out_dir="/dl",
        debrid_route="torbox",
        debrid_item_id="42",
        debrid_file_id="",
        resolved_url="https://cdn-01.torbox.app/dl/secret/f.zip",
        debrid_provider="torbox",
    )
    snapshot = task_snapshot(task)
    for forbidden in (
        "debrid_route", "debrid_item_id", "debrid_file_id",
        "resolved_url", "debrid_provider",
    ):
        assert forbidden not in snapshot
    snapshot_text = json.dumps(snapshot)
    assert "torbox" not in snapshot_text
    assert "secret" not in snapshot_text


def test_torbox_settings_do_not_broaden_the_add_payload_schema(api_server):
    """TorBox must not add any new accepted field to POST /downloads: an
    internal identifier submitted by a client is still an unknown field."""
    server, _bridge = api_server
    status, payload = call(
        server, "POST", "/api/v1/downloads",
        body={
            "url": "https://example.com/f.bin",
            "debrid_route": "torbox",
            "debrid_item_id": "42",
            "torbox_api_token": "torbox-token-value",
        },
        headers=auth(),
    )
    assert status == 400
    assert payload["error"]["code"] == "unknown_fields"
