"""Authenticated, versioned loopback API for Cove Download Manager.

HTTP requests run on worker threads.  Every QueueManager read and mutation is
marshalled to its owning Qt thread through :class:`QueueApiBridge`.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from . import __version__
from .config import MAX_CONNECTIONS_PER_SERVER, Settings
from .output_paths import OutputPathError, validate_public_filename
from .queue import DownloadTask, QueueManager

API_PREFIX = "/api/v1"
MAX_BODY_BYTES = 16 * 1024
BRIDGE_TIMEOUT_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 5.0
ALLOWED_URL_SCHEMES = {"http", "https", "ftp", "magnet"}
_TASK_PATH_RE = re.compile(r"^/api/v1/downloads/([^/]+)(?:/(pause|resume|cancel))?$")
class ApiProblem(Exception):
    """A stable API error safe to serialize to a client."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _problem(status: int, code: str, message: str) -> ApiProblem:
    return ApiProblem(status, code, message)


def validate_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _problem(400, "invalid_url", "A non-empty download URL is required.")
    value = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _problem(400, "invalid_url", "Control characters are not allowed in URLs.")
    if any(char.isspace() for char in value):
        raise _problem(400, "invalid_url", "Whitespace is not allowed in URLs.")
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise _problem(400, "invalid_url", "The URL is malformed.") from exc
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_URL_SCHEMES:
        raise _problem(400, "invalid_url", "The URL scheme is not supported by Cove.")
    if scheme in {"http", "https", "ftp"} and not parsed.netloc:
        raise _problem(400, "invalid_url", "The URL must include a host.")
    if scheme == "magnet" and not parsed.query:
        raise _problem(400, "invalid_url", "The magnet URI has no query payload.")
    return value


def validate_filename(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return validate_public_filename(value)
    except OutputPathError as exc:
        raise _problem(400, "invalid_filename", "The filename is invalid.") from exc


def validate_directory(value: Any, create: bool) -> str | None:
    if value is None:
        if create:
            raise _problem(400, "invalid_create_directory", "create_directory requires an explicit directory.")
        return None
    if not isinstance(value, str) or not value:
        raise _problem(400, "invalid_directory", "The directory must be a non-empty absolute path.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _problem(400, "invalid_directory", "Control characters are not allowed in directory paths.")
    expanded = Path(os.path.expandvars(value)).expanduser()
    if not expanded.is_absolute():
        raise _problem(400, "directory_not_absolute", "The download directory must be absolute.")
    path = expanded.resolve(strict=False)
    if path.exists() and not path.is_dir():
        raise _problem(400, "directory_invalid", "The destination exists but is not a directory.")
    if not path.exists():
        if not create:
            raise _problem(400, "directory_not_found", "The destination directory does not exist.")
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _problem(400, "directory_create_failed", f"Could not create the destination: {exc}") from exc
    return str(path)


def validate_connections(value: Any) -> int | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_CONNECTIONS_PER_SERVER
    ):
        raise _problem(
            400,
            "invalid_connections",
            f"Connections must be an integer between 1 and {MAX_CONNECTIONS_PER_SERVER}.",
        )
    return value


def validate_speed_limit(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2_147_483_647:
        raise _problem(400, "invalid_speed_limit", "speed_limit_kbps must be a non-negative integer.")
    return value


def validate_add_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _problem(400, "invalid_json", "The JSON body must be an object.")
    allowed = {"url", "directory", "filename", "connections", "speed_limit_kbps", "create_directory"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise _problem(400, "unknown_fields", f"Unknown request field: {unknown[0]}.")
    create = payload.get("create_directory", False)
    if not isinstance(create, bool):
        raise _problem(400, "invalid_create_directory", "create_directory must be a boolean.")
    url = validate_url(payload.get("url"))
    if "connections" in payload or "speed_limit_kbps" in payload:
        # ffmpeg/yt-dlp downloads never receive per-download connection or
        # speed-limit options, so refuse explicit ones rather than
        # acknowledging controls that would silently not apply.
        from .extractor import is_extractor_url
        from .hls import is_hls_url
        if is_hls_url(url) or is_extractor_url(url):
            raise _problem(
                400,
                "unsupported_for_backend",
                "connections and speed_limit_kbps only apply to direct downloads, "
                "not video or stream URLs.",
            )
    return {
        "url": url,
        "directory": validate_directory(payload.get("directory"), create),
        "filename": validate_filename(payload.get("filename")),
        "connections": validate_connections(payload.get("connections")),
        "speed_limit_kbps": validate_speed_limit(payload.get("speed_limit_kbps")),
    }


def task_snapshot(task: DownloadTask) -> dict[str, Any]:
    total = max(0, int(task.total_bytes or 0))
    completed = max(0, int(task.completed_bytes or 0))
    progress = round(completed * 100.0 / total, 2) if total else 0.0
    return {
        "task_id": int(task.id),
        "gid": task.gid or None,
        "url": task.url,
        "filename": task.filename or None,
        "directory": task.out_dir,
        "status": task.status,
        "backend": task.backend,
        "connections": int(task.connections),
        "speed_limit_kbps": int(task.speed_limit_kbps),
        "completed_bytes": completed,
        "total_bytes": total,
        "speed_bytes_per_second": max(0, int(task.download_speed or 0)),
        "progress_percent": progress,
        "error": task.error or None,
        "created_at": task.created_at,
        "finished_at": task.finished_at,
    }


@dataclass
class _BridgeRequest:
    action: str
    payload: dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _abandoned: bool = False
    _started: bool = False

    def try_start(self) -> bool:
        """Claim the request for execution; False if the caller gave up."""
        with self._lock:
            if self._abandoned:
                return False
            self._started = True
            return True

    def abandon(self) -> bool:
        """Mark the request expired; False if execution already started."""
        with self._lock:
            if self._started:
                return False
            self._abandoned = True
            return True


class QueueApiBridge(QObject):
    """Serialize API access onto QueueManager's Qt thread."""

    _dispatch = Signal(object)

    def __init__(self, queue: QueueManager, timeout: float = BRIDGE_TIMEOUT_SECONDS) -> None:
        super().__init__(queue)
        self.queue = queue
        self.timeout = timeout
        self._removed_task_ids: set[int] = set()
        self._dispatch.connect(self._execute, Qt.QueuedConnection)

    def invoke(self, action: str, payload: dict[str, Any] | None = None) -> Any:
        request = _BridgeRequest(action, payload or {})
        if QThread.currentThread() is self.thread():
            self._execute(request)
        else:
            self._dispatch.emit(request)
            if not request.event.wait(self.timeout):
                # A timed-out request must never mutate the queue afterwards:
                # abandon it so _execute() skips it. If execution already
                # started, wait for the outcome so the response stays truthful.
                if request.abandon():
                    raise _problem(503, "bridge_timeout", "Cove's main thread did not answer in time.")
                request.event.wait()
        if request.error is not None:
            raise request.error
        return request.result

    @Slot(object)
    def _execute(self, request: _BridgeRequest) -> None:
        if not request.try_start():
            return
        try:
            request.result = self._handle(request.action, request.payload)
        except BaseException as exc:  # carried back to the bounded HTTP worker
            request.error = exc
        finally:
            request.event.set()

    def _task(self, task_id: int) -> DownloadTask:
        if task_id in self._removed_task_ids:
            raise _problem(404, "task_not_found", f"No Cove task has ID {task_id}.")
        task = self.queue.tasks.get(task_id)
        if task is None:
            raise _problem(404, "task_not_found", f"No Cove task has ID {task_id}.")
        return task

    def _handle(self, action: str, payload: dict[str, Any]) -> Any:
        if action == "settings":
            settings = self.queue.settings
            return {
                "download_directory": settings.download_dir,
                "connections_per_download": settings.connections_per_server,
                "max_concurrent": settings.max_concurrent,
                "schedule_enabled": settings.schedule.enabled,
                "auto_sort_by_category": settings.auto_sort_by_category,
                "api_enabled": settings.api_enabled,
                "api_port": settings.api_port,
            }
        if action == "add":
            task_id = self.queue.add_url(
                payload["url"],
                out_dir=payload.get("directory"),
                filename=payload.get("filename"),
                connections=payload.get("connections"),
                speed_limit_kbps=payload.get("speed_limit_kbps", 0),
            )
            if task_id is None:
                raise _problem(409, "download_rejected", "Cove could not accept the download.")
            return task_snapshot(self._task(task_id))
        if action == "list":
            tasks = sorted(
                (task for task in self.queue.tasks.values() if task.id not in self._removed_task_ids),
                key=lambda task: (task.created_at, task.id),
            )
            return [task_snapshot(task) for task in tasks]

        task_id = payload["task_id"]
        task = self._task(task_id)
        if action == "status":
            return task_snapshot(task)
        if action == "pause":
            self.queue.pause(task_id)
            return task_snapshot(self._task(task_id))
        if action == "resume":
            self.queue.resume(task_id)
            return task_snapshot(self._task(task_id))
        if action == "cancel":
            snapshot = task_snapshot(task)
            # keep_incomplete pins the API's existing contract: cancelling a
            # task through the API removes the row and never touches disk.
            self.queue.remove(task_id, delete_file=False, keep_incomplete=True)
            # Keep only IDs the queue still tracks (the in-flight add_uri
            # remove case); anything else 404s on its own, so pruning here
            # stops the set from growing for the life of the session.
            self._removed_task_ids.intersection_update(self.queue.tasks.keys())
            self._removed_task_ids.add(task_id)
            snapshot.update({"status": "removed", "speed_bytes_per_second": 0})
            return snapshot
        raise _problem(500, "internal_error", "Unsupported bridge operation.")


class _ApiHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, service: "LocalApiServer") -> None:
        self.service = service
        super().__init__(address, handler)


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CoveLocalAPI/1"
    sys_version = ""

    @property
    def service(self) -> "LocalApiServer":
        return self.server.service  # type: ignore[attr-defined]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def log_message(self, _format: str, *_args: Any) -> None:
        # Do not let headers, URLs, or future credentials reach stderr.
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            # A client that stops reading is the same as one that hung up:
            # drop the response instead of raising out of the handler.
            pass

    def _fail(self, problem: ApiProblem) -> None:
        self._send(problem.status, {"ok": False, "error": {"code": problem.code, "message": problem.message}})

    def _reject_origin(self) -> None:
        if self.headers.get("Origin") is not None:
            raise _problem(403, "origin_not_allowed", "Browser-origin requests are not allowed.")

    def _authenticate(self) -> None:
        values = self.headers.get_all("Authorization") or []
        if not values:
            raise _problem(401, "missing_auth", "A bearer token is required.")
        scheme, _, token = values[0].partition(" ")
        # Auth scheme names are case-insensitive (RFC 7235).
        if len(values) != 1 or scheme.lower() != "bearer" or not token:
            raise _problem(401, "malformed_auth", "Authorization must use a bearer token.")
        if not secrets.compare_digest(token, self.service.token):
            raise _problem(401, "invalid_token", "The bearer token is incorrect.")

    def _json_body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding") is not None:
            raise _problem(400, "unsupported_transfer_encoding", "Chunked request bodies are not supported.")
        if self.headers.get_content_type() != "application/json":
            raise _problem(415, "unsupported_content_type", "Content-Type must be application/json.")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise _problem(411, "content_length_required", "Content-Length is required.")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise _problem(400, "invalid_content_length", "Content-Length is invalid.") from exc
        if length < 0:
            raise _problem(400, "invalid_content_length", "Content-Length is invalid.")
        if length > MAX_BODY_BYTES:
            raise _problem(413, "body_too_large", f"Request bodies are limited to {MAX_BODY_BYTES} bytes.")
        try:
            body = self.rfile.read(length)
        except (TimeoutError, socket.timeout) as exc:
            raise _problem(408, "request_timeout", "The request body was not received in time.") from exc
        if len(body) != length:
            raise _problem(400, "incomplete_body", "The request body ended before Content-Length bytes arrived.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _problem(400, "malformed_json", "The request body is not valid UTF-8 JSON.") from exc
        if not isinstance(payload, dict):
            raise _problem(400, "invalid_json", "The JSON body must be an object.")
        return payload

    def _route(self, method: str) -> None:
        try:
            self._reject_origin()
            parsed = urlsplit(self.path)
            path = parsed.path
            if parsed.query:
                raise _problem(400, "query_not_allowed", "Query parameters are not supported.")
            if method == "GET" and path == f"{API_PREFIX}/health":
                self._send(200, {"ok": True, "service": "cove", "api_version": "v1", "cove_version": __version__})
                return

            self._authenticate()
            if method == "GET" and path == f"{API_PREFIX}/settings":
                self._send(200, {"ok": True, "settings": self.service.bridge.invoke("settings")})
                return
            if method == "GET" and path == f"{API_PREFIX}/downloads":
                downloads = self.service.bridge.invoke("list")
                self._send(200, {"ok": True, "count": len(downloads), "downloads": downloads})
                return
            if method == "POST" and path == f"{API_PREFIX}/downloads":
                download = self.service.bridge.invoke("add", validate_add_payload(self._json_body()))
                self._send(202, {"ok": True, "download": download})
                return

            match = _TASK_PATH_RE.fullmatch(path)
            if match:
                raw_task_id = match.group(1)
                if (
                    len(raw_task_id) > 18
                    or not raw_task_id.isascii()
                    or not raw_task_id.isdigit()
                    or int(raw_task_id) <= 0
                ):
                    raise _problem(400, "invalid_task_id", "Cove task IDs must be positive integers.")
                task_id = int(raw_task_id)
                operation = match.group(2)
                if method == "GET" and operation is None:
                    download = self.service.bridge.invoke("status", {"task_id": task_id})
                    self._send(200, {"ok": True, "download": download})
                    return
                if method == "POST" and operation in {"pause", "resume", "cancel"}:
                    body = self._json_body()
                    if body:
                        raise _problem(400, "unknown_fields", "This operation accepts only an empty JSON object.")
                    download = self.service.bridge.invoke(operation, {"task_id": task_id})
                    self._send(200, {"ok": True, "download": download})
                    return
                raise _problem(405, "method_not_allowed", "The HTTP method is not allowed for this endpoint.")
            if path.startswith(API_PREFIX):
                raise _problem(404, "endpoint_not_found", "The API endpoint does not exist.")
            raise _problem(404, "not_found", "Not found.")
        except ApiProblem as problem:
            self._fail(problem)
        except Exception:
            self._fail(_problem(500, "internal_error", "Cove could not complete the API request."))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._route("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._route("OPTIONS")


class LocalApiServer:
    """Own the loopback HTTP server thread and its Qt bridge."""

    def __init__(
        self,
        settings: Settings,
        queue: QueueManager,
        *,
        port: int | None = None,
        bridge: QueueApiBridge | None = None,
    ) -> None:
        self.settings = settings
        self.token = settings.api_token
        self.port = settings.api_port if port is None else port
        self.bridge = bridge or QueueApiBridge(queue)
        self._server: _ApiHttpServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def bound_port(self) -> int | None:
        return self._server.server_address[1] if self._server is not None else None

    def start(self) -> None:
        if self._server is not None:
            return
        if not isinstance(self.token, str) or len(self.token) < 24:
            raise ValueError("Cove API token is missing or invalid")
        server = _ApiHttpServer(("127.0.0.1", self.port), _RequestHandler, self)
        thread = threading.Thread(target=server.serve_forever, name="cove-local-api", daemon=True)
        self._server = server
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        server.shutdown()
        server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
