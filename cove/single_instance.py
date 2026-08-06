"""Local single-instance IPC: election, framing, and forwarding.

A second Cove launch must not construct its own Settings/aria2/queue/window
stack - two aria2 daemons fight over the same RPC port. This module lets the
first process become "primary" (it owns a local socket endpoint keyed to its
data directory) and lets any later process detect that, forward its magnet
URLs to the primary, and exit before touching anything stateful.

Kept dependency-free (stdlib + the Qt local-IPC classes cove already ships
with) and deliberately shallow: this module only proves a message is
well-formed enough to hand to `QueueManager.add_url`. It never logs payload
contents - a magnet's tracker list can carry a private passkey.
"""
from __future__ import annotations

import hashlib
import json
import logging
import struct
import tempfile
import time
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QCoreApplication, QLockFile, QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from .torrent import MAX_MAGNET_LENGTH, is_magnet

logger = logging.getLogger("cove.single_instance")

PROTOCOL_VERSION = 1
MAX_URLS_PER_MESSAGE = 20
MAX_MESSAGE_BYTES = 200 * 1024  # 200 KiB
LENGTH_PREFIX_SIZE = 4

_DEFAULT_CONNECT_TIMEOUT_MS = 1500
_DEFAULT_ACK_TIMEOUT_MS = 1500
_STALE_PROBE_TIMEOUT_MS = 500
_ELECTION_LOCK_TIMEOUT_MS = 2000
_ELECTION_LOCK_STALE_MS = 5000
_CONNECTION_READ_TIMEOUT_MS = 5000

# Bounds for the browser-download action. A web page controls the URL and
# (indirectly) the cookie jar behind these values, so every field is capped
# before it can reach the queue or grow the IPC frame.
MAX_BROWSER_URL_LENGTH = 8 * 1024
MAX_BROWSER_FILENAME_LENGTH = 512
MAX_BROWSER_DIRECTORY_LENGTH = 4096
MAX_BROWSER_COOKIES_LENGTH = 32 * 1024
MAX_BROWSER_REFERRER_LENGTH = 8 * 1024
MAX_BROWSER_USER_AGENT_LENGTH = 1024
MAX_BROWSER_FILE_SIZE = 1 << 62

# Schemes the browser extension may hand over. Deliberately the same direct
# schemes native messaging already accepted - magnets and local schemes are
# not browser-interception material and must not arrive by this route.
BROWSER_URL_SCHEMES = ("http://", "https://", "ftp://")

_ERROR_SENTENCES = {
    "unsupported_version": "This request uses an unsupported protocol version.",
    "rejected": "This request was not accepted.",
    "unknown_action": "This request uses an unsupported action.",
    "malformed_json": "This request could not be read.",
    "invalid_schema": "This request is not in the expected format.",
    "too_many_urls": "This request contains too many links.",
    "invalid_url": "This request contains an unsupported link.",
    "oversized_message": "This request is too large.",
    "truncated_message": "This request was incomplete.",
}
_DEFAULT_ERROR = "This request could not be processed."


class MessageError(Exception):
    """A request failed shallow IPC validation. `category` is a fixed,
    sanitized label safe to log or return to the sender - never the reason
    text a user might read back their own payload from."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def server_name(data_dir: "str | Path") -> str:
    """Deterministic, bounded local-endpoint name for a data directory.

    Hashed so the name never leaks the filesystem path, and derived from the
    resolved data directory (not the username alone) so two portable installs
    with distinct data directories never collide.
    """
    resolved = str(Path(data_dir).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"cove-{digest[:16]}"


def _election_lock_path(name: str) -> Path:
    """Path for the short-lived mutex serializing election for `name`.

    Deliberately separate from the socket path itself (and from any
    per-install data directory, which may not exist yet this early in
    startup) - a plain OS temp-dir file keyed by the already-bounded,
    already-hashed `name`.
    """
    return Path(tempfile.gettempdir()) / f"{name}.election.lock"


def _is_control_free(value: str) -> bool:
    return all(ord(ch) >= 32 and ch != "\x7f" for ch in value)


def is_valid_launch_url(value) -> bool:
    """One shared validation policy for a single launch URL.

    Used identically for command-line GUI arguments and IPC `open` message
    URLs, so the same magnet is accepted or rejected the same way
    regardless of which path it arrives through: non-empty string, within
    `torrent.MAX_MAGNET_LENGTH`, free of NUL/control characters, and
    passing the existing cheap magnet-prefix gate (`torrent.is_magnet`).
    Deep validation (info-hash parsing, provider routing, ...) is
    deliberately not done here - it stays entirely inside the queue/torrent
    path, reached only via `queue.add_url`.
    """
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= MAX_MAGNET_LENGTH
        and _is_control_free(value)
        and is_magnet(value)
    )


def _validate_urls(raw_urls) -> list[str]:
    if not isinstance(raw_urls, list):
        raise MessageError("invalid_schema")
    if len(raw_urls) > MAX_URLS_PER_MESSAGE:
        raise MessageError("too_many_urls")
    urls: list[str] = []
    for item in raw_urls:
        if not is_valid_launch_url(item):
            raise MessageError("invalid_url")
        urls.append(item)
    return urls


def decode_payload(payload: bytes) -> dict:
    """Parse the JSON body of a frame. Raises MessageError("malformed_json")
    on anything that isn't valid UTF-8 JSON."""
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise MessageError("malformed_json") from None


def validate_message(obj) -> tuple[str, list[str]]:
    """Shallow-validate a decoded IPC request. Returns (action, urls).

    Deep magnet validation (info-hash parsing, provider routing, ...) stays
    entirely inside the existing queue/torrent path - this only decides
    whether the message is safe to hand off.
    """
    if not isinstance(obj, dict):
        raise MessageError("invalid_schema")
    if obj.get("version") != PROTOCOL_VERSION:
        raise MessageError("unsupported_version")
    action = obj.get("action")
    if action not in ("open", "activate"):
        raise MessageError("unknown_action")
    if "urls" not in obj:
        raise MessageError("invalid_schema")
    urls = _validate_urls(obj["urls"])
    if action == "open" and not urls:
        raise MessageError("invalid_schema")
    if action == "activate" and urls:
        # An `activate` request never hands URLs to open_requested - the
        # primary must not be able to claim it "accepted" URLs it is about
        # to silently discard. Reject the whole request instead of
        # accepting-and-dropping some of it.
        raise MessageError("invalid_schema")
    return action, urls


BROWSER_DOWNLOAD_ACTION = "browser_download"

# A "the extension just talked to me" heartbeat from the native host. It
# carries no payload and never activates the window: the browser sends it on
# its own schedule, so treating it as user intent would raise Cove unbidden.
EXTENSION_PING_ACTION = "extension_ping"


def _sanitize_header(value, limit: int) -> str:
    """Bounded, CR/LF-free copy of an optional browser-supplied header.

    CR/LF removal stops an extension- or page-controlled value from injecting
    extra request headers into the fetch the backend later performs; the
    length bound stops one page from inflating the IPC frame (and, later, an
    aria2 command line) without limit. Anything that isn't a string - or that
    exceeds the bound, or carries other control characters - is a rejected
    request, not a silently trimmed one, so the browser keeps its download
    rather than Cove taking it with headers that no longer authenticate.
    """
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise MessageError("invalid_schema")
    if len(value) > limit:
        raise MessageError("oversized_message")
    cleaned = value.replace("\r", "").replace("\n", "")
    if not _is_control_free(cleaned):
        raise MessageError("invalid_schema")
    return cleaned


def validate_browser_download(obj: dict) -> dict:
    """Shallow-validate a `browser_download` request into queue arguments.

    Returns the exact keyword material `QueueManager.add_url` needs. Deep
    resolution (debrid routing, category routing, HLS/extractor detection)
    stays where it already lives, inside `add_url` itself - this only decides
    whether the request is safe and bounded enough to hand over.
    """
    url = obj.get("url")
    if not isinstance(url, str):
        raise MessageError("invalid_url")
    url = url.strip()
    if (
        not url
        or len(url) > MAX_BROWSER_URL_LENGTH
        or not _is_control_free(url)
        or not url.lower().startswith(BROWSER_URL_SCHEMES)
    ):
        raise MessageError("invalid_url")

    filename = _sanitize_header(obj.get("filename"), MAX_BROWSER_FILENAME_LENGTH)
    directory = _sanitize_header(obj.get("directory"), MAX_BROWSER_DIRECTORY_LENGTH)

    file_size = obj.get("file_size", obj.get("fileSize", 0))
    if file_size in (None, ""):
        file_size = 0
    if isinstance(file_size, bool) or not isinstance(file_size, int):
        raise MessageError("invalid_schema")
    if file_size < 0 or file_size > MAX_BROWSER_FILE_SIZE:
        raise MessageError("invalid_schema")

    return {
        "url": url,
        # "" means "no explicit choice": the queue's own category routing and
        # default download directory still apply.
        "filename": filename or None,
        "directory": directory or None,
        "cookies": _sanitize_header(
            obj.get("cookies"), MAX_BROWSER_COOKIES_LENGTH
        ),
        "referrer": _sanitize_header(
            obj.get("referrer"), MAX_BROWSER_REFERRER_LENGTH
        ),
        "user_agent": _sanitize_header(
            obj.get("user_agent", obj.get("userAgent")),
            MAX_BROWSER_USER_AGENT_LENGTH,
        ),
        "file_size": file_size,
    }


def validate_request(obj) -> tuple[str, dict]:
    """Route one decoded IPC request to the validator for its action.

    `open`/`activate` keep `validate_message` unchanged (magnet policy);
    `browser_download` gets its own bounded validator. Returns
    (action, payload) where payload is `{"urls": [...]}` for the magnet
    actions and the queue arguments for a browser download.
    """
    if not isinstance(obj, dict):
        raise MessageError("invalid_schema")
    if obj.get("action") == BROWSER_DOWNLOAD_ACTION:
        if obj.get("version") != PROTOCOL_VERSION:
            raise MessageError("unsupported_version")
        return BROWSER_DOWNLOAD_ACTION, validate_browser_download(obj)
    if obj.get("action") == EXTENSION_PING_ACTION:
        if obj.get("version") != PROTOCOL_VERSION:
            raise MessageError("unsupported_version")
        # Payload-free by contract: anything carrying extra fields is a
        # different message, not a heartbeat, and must not be accepted as one.
        if set(obj) != {"version", "action"}:
            raise MessageError("invalid_schema")
        return EXTENSION_PING_ACTION, {}
    action, urls = validate_message(obj)
    return action, {"urls": urls}


def encode_message(obj: dict) -> bytes:
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise MessageError("oversized_message")
    return struct.pack(">I", len(payload)) + payload


def _error_sentence(category: str) -> str:
    return _ERROR_SENTENCES.get(category, _DEFAULT_ERROR)


class _PendingConnection(QObject):
    """Bounded-read state machine for one accepted server-side connection.

    Exactly one request/response round trip per connection: after the first
    complete frame is handled the connection is closed, and any trailing
    bytes after that frame are discarded rather than parsed as a second
    message (deterministic single-message-per-connection framing).
    """

    def __init__(
        self,
        socket: QLocalSocket,
        server: "SingleInstanceServer",
        read_timeout_ms: int = _CONNECTION_READ_TIMEOUT_MS,
    ) -> None:
        super().__init__(server)
        self._socket = socket
        self._server = server
        self._buf = bytearray()
        self._expected_len: int | None = None
        self._done = False
        self._finished = False
        socket.setParent(self)
        socket.readyRead.connect(self._on_ready_read)
        socket.disconnected.connect(self._cleanup)
        socket.errorOccurred.connect(lambda *_: self._cleanup())

        # Bounded read deadline: a same-user client that connects and never
        # completes a framed request (nothing sent, a partial length
        # prefix, or a declared payload that never fully arrives) would
        # otherwise hold this connection - and its socket - open forever.
        # Started as soon as the socket is accepted; stopped as soon as we
        # have a final outcome (a response was sent, or the socket is gone)
        # so it never fires for a connection that already finished cleanly.
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._on_timeout)
        self._timeout_timer.start(read_timeout_ms)

        # The client may have already written (and we may already have
        # buffered) data by the time nextPendingConnection() hands us this
        # socket - readyRead only fires for data that arrives *after* this
        # point, so drain whatever is already sitting there right away.
        if socket.bytesAvailable():
            self._on_ready_read()

    def _on_timeout(self) -> None:
        if self._finished:
            return
        # Fixed event name only - never the partial buffer contents, which
        # may hold an in-progress magnet.
        logger.info("ipc_connection_timeout")
        self._done = True
        self._socket.abort()
        self._cleanup()

    def _on_ready_read(self) -> None:
        if self._done:
            return
        self._buf.extend(bytes(self._socket.readAll()))
        if self._expected_len is None:
            if len(self._buf) < LENGTH_PREFIX_SIZE:
                if len(self._buf) > MAX_MESSAGE_BYTES:
                    self._respond_error("oversized_message")
                return
            (length,) = struct.unpack(">I", bytes(self._buf[:LENGTH_PREFIX_SIZE]))
            if length > MAX_MESSAGE_BYTES:
                self._respond_error("oversized_message")
                return
            self._expected_len = length
            del self._buf[:LENGTH_PREFIX_SIZE]
        if len(self._buf) < self._expected_len:
            return
        payload = bytes(self._buf[: self._expected_len])
        self._handle_payload(payload)

    def _handle_payload(self, payload: bytes) -> None:
        self._done = True
        try:
            obj = decode_payload(payload)
            action, request = validate_request(obj)
        except MessageError as exc:
            logger.info("ipc_rejected category=%s", exc.category)
            self._respond_error(exc.category)
            return

        if action == BROWSER_DOWNLOAD_ACTION:
            self._handle_browser_download(request)
            return

        if action == EXTENSION_PING_ACTION:
            logger.info("ipc_accepted action=%s", action)
            self._server.extension_seen.emit()
            self._respond_ok(0)
            return

        urls = request["urls"]
        logger.info("ipc_accepted action=%s url_count=%d", action, len(urls))
        if action == "open":
            self._server.open_requested.emit(urls)
        self._server.activate_requested.emit()
        self._respond_ok(len(urls))

    def _handle_browser_download(self, request: dict) -> None:
        """Answer ok only once the *running* primary has actually taken the
        download. The browser cancels its own transfer on this answer, so a
        signal-and-hope emit would be a lie: Signal delivery says nothing
        about whether the queue accepted anything. A plain callable is used
        instead precisely because it returns a value.

        Every failure mode - no handler yet (queue not ready), handler
        declined, handler raised - answers ok=False, which leaves the browser
        responsible for its own download. Nothing is buffered for later.
        """
        handler = self._server.browser_download_handler
        accepted = False
        # extension_seen is emitted only once acceptance is known, so the
        # indicator never claims a connection off a request this process
        # refused. The heartbeat is what proves presence in that case.
        if handler is not None:
            try:
                accepted = bool(handler(request))
            except Exception:
                # Fixed event name only: the request holds a URL, cookies and
                # a referrer, none of which may reach the log.
                logger.info("ipc_browser_download_handler_error")
                accepted = False
        # An automatically captured download must not raise the window, so
        # activate_requested is deliberately not emitted here.
        logger.info("ipc_browser_download accepted=%s", accepted)
        if accepted:
            self._server.extension_seen.emit()
            self._respond_ok(1)
        else:
            self._respond_error("rejected")

    def _respond_ok(self, accepted: int) -> None:
        self._write({"version": PROTOCOL_VERSION, "ok": True, "accepted": accepted})

    def _respond_error(self, category: str) -> None:
        self._done = True
        self._write(
            {"version": PROTOCOL_VERSION, "ok": False, "error": _error_sentence(category)}
        )

    def _write(self, obj: dict) -> None:
        # A response is about to be sent (or attempted) - the read deadline
        # no longer applies to this connection either way.
        self._timeout_timer.stop()
        try:
            self._socket.write(encode_message(obj))
            self._socket.flush()
            self._socket.disconnectFromServer()
        except (MessageError, OSError):
            pass

    def _cleanup(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._timeout_timer.stop()
        self._socket.deleteLater()
        self.deleteLater()


class SingleInstanceServer(QObject):
    """Owns (at most) one local IPC endpoint for this process."""

    open_requested = Signal(list)
    activate_requested = Signal()
    # The browser extension reached this process, via a heartbeat or a
    # download. Presence only - it carries nothing about the request.
    extension_seen = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        connection_read_timeout_ms: int = _CONNECTION_READ_TIMEOUT_MS,
    ) -> None:
        # Default to the running QCoreApplication so Qt - not Python
        # refcounting - owns this object and everything parented under it
        # (the QLocalServer, accepted connections, their sockets). Without a
        # C++ parent, Python could synchronously free the whole tree the
        # moment the last reference drops, while a deleteLater() queued for
        # one of its descendants (e.g. a just-served _PendingConnection) is
        # still pending - the deferred event then fires on freed memory.
        if parent is None:
            parent = QCoreApplication.instance()
        super().__init__(parent)
        self._server: QLocalServer | None = None
        self._name: str | None = None
        self._owned = False
        self._connection_read_timeout_ms = connection_read_timeout_ms
        # Installed by the running primary once its queue can actually accept
        # downloads, and cleared again on shutdown. `None` means "this
        # process cannot take a browser download right now" - the request is
        # refused rather than buffered, so the browser keeps its transfer.
        self.browser_download_handler = None

    def try_become_primary(self, name: str) -> bool:
        """Attempt to claim `name`. Returns True iff this process is primary.

        `QLocalServer.listen()` cannot be trusted to fail on its own when
        another process already owns `name`: on Unix it will silently
        unlink a pre-existing socket path (live or stale) and bind its own,
        which would corrupt a running primary. So liveness is proven first,
        by connecting - only once that connection attempt fails (bounded
        timeout) do we treat the path as stale and clean it up before ever
        calling `listen()`. A live primary's endpoint is therefore never
        touched, and `listen()` is attempted at most twice (once, plus one
        retry after the validated cleanup).

        The whole probe -> cleanup -> listen sequence is itself wrapped in
        a short-lived, cross-platform `QLockFile` mutex keyed to `name`
        (bounded wait, no polling loop of our own). Without it, two Cove
        processes starting at nearly the same instant could both observe
        "nothing answered", and the second one's `removeServer()` could
        unlink the socket path the first one had *just* bound - corrupting
        a fresh primary instead of a stale one. The lock is released before
        this call returns either way; it does not need to be held for the
        primary's whole lifetime, since once one process is listening, any
        later probe correctly finds it alive.
        """
        self._name = name
        election_lock = QLockFile(str(_election_lock_path(name)))
        election_lock.setStaleLockTime(_ELECTION_LOCK_STALE_MS)
        if not election_lock.tryLock(_ELECTION_LOCK_TIMEOUT_MS):
            return False  # another launch is mid-election for this name right now

        try:
            if not self._probe_dead(name):
                return False  # something answered: a live primary owns this name

            # Nothing answered: any leftover socket path is stale left over
            # from a crash, not a live server. Safe to clear before binding.
            QLocalServer.removeServer(name)

            server = QLocalServer(self)
            server.setSocketOptions(QLocalServer.UserAccessOption)
            if server.listen(name):
                self._install(server)
                return True

            # One bounded retry in case of a transient failure right after
            # cleanup. `server` stays parented to us (Qt, not Python, owns
            # it now) so it is cleaned up when we are - no deleteLater()
            # needed, and calling it here would race Qt's own destruction
            # of this object during interpreter shutdown or test teardown.
            retry = QLocalServer(self)
            retry.setSocketOptions(QLocalServer.UserAccessOption)
            if retry.listen(name):
                self._install(retry)
                return True
            return False
        finally:
            election_lock.unlock()

    def _probe_dead(self, name: str) -> bool:
        """True if nothing accepted a connection within a bounded timeout."""
        sock = QLocalSocket()
        sock.connectToServer(name)
        connected = sock.waitForConnected(_STALE_PROBE_TIMEOUT_MS)
        sock.abort()
        return not connected

    def _install(self, server: QLocalServer) -> None:
        self._server = server
        self._owned = True
        server.newConnection.connect(self._on_new_connection)
        logger.info("ipc_server_listening")

    def _on_new_connection(self) -> None:
        assert self._server is not None
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                break
            _PendingConnection(conn, self, self._connection_read_timeout_ms)

    def shutdown(self) -> None:
        """Close and remove only the endpoint this process owns.

        `self._server` stays parented to `self` for its whole life, so Qt
        (not Python) owns it; closing it here is enough; deleteLater()
        would queue an async C++ deletion event that can outlive this
        object and crash if `self` is torn down first.

        Closing and removing the endpoint is wrapped in the same election
        mutex used by `try_become_primary`: without it, another process's
        connect-probe could run *between* our `close()` and our
        `removeServer()`, correctly conclude we are dead, and successfully
        bind a brand new primary - which our own `removeServer()` would
        then unlink out from under it.
        """
        # Refuse browser downloads from this instant on: shutdown has begun,
        # so anything accepted now would never be driven to completion.
        self.browser_download_handler = None
        if not self._owned or not self._name:
            self._server = None
            return
        election_lock = QLockFile(str(_election_lock_path(self._name)))
        election_lock.setStaleLockTime(_ELECTION_LOCK_STALE_MS)
        held = election_lock.tryLock(_ELECTION_LOCK_TIMEOUT_MS)
        try:
            # Stopping our own listener is always safe - we own it either way.
            if self._server is not None:
                self._server.close()
                self._server = None
            # But only unlink the shared endpoint name while holding the
            # mutex: without it, a contender could be between its own
            # connect-probe and listen() right now, and an unprotected
            # removeServer() here could unlink the fresh socket it just
            # bound instead of the stale one it correctly identified us as.
            if held:
                QLocalServer.removeServer(self._name)
            self._owned = False
        finally:
            if held:
                election_lock.unlock()


def send_to_primary(
    name: str,
    urls: Iterable[str],
    connect_timeout_ms: int = _DEFAULT_CONNECT_TIMEOUT_MS,
    ack_timeout_ms: int = _DEFAULT_ACK_TIMEOUT_MS,
) -> bool:
    """Forward `urls` (empty => activate-only) to the primary. Never raises."""
    url_list = list(urls)
    action = "open" if url_list else "activate"
    return _request(
        name,
        {"version": PROTOCOL_VERSION, "action": action, "urls": url_list},
        connect_timeout_ms,
        ack_timeout_ms,
    )


def send_browser_download(
    name: str,
    request: dict,
    connect_timeout_ms: int = _DEFAULT_CONNECT_TIMEOUT_MS,
    ack_timeout_ms: int = _DEFAULT_ACK_TIMEOUT_MS,
) -> bool:
    """Ask the running primary to take one browser download. Never raises.

    True means a Cove process that is running *right now* validated the
    request and its queue accepted it. Every other outcome - no primary
    listening, a primary whose queue isn't ready, a rejected add, a timeout,
    a malformed reply - returns False, and the caller must leave the browser's
    own download alone. Nothing is written anywhere on failure, so a later
    Cove launch can never inherit this request.
    """
    message = {"version": PROTOCOL_VERSION, "action": BROWSER_DOWNLOAD_ACTION}
    for key in (
        "url",
        "filename",
        "directory",
        "cookies",
        "referrer",
        "user_agent",
        "file_size",
    ):
        if request.get(key) not in (None, ""):
            message[key] = request[key]
    return _request(name, message, connect_timeout_ms, ack_timeout_ms)


def send_extension_ping(
    name: str,
    connect_timeout_ms: int = _DEFAULT_CONNECT_TIMEOUT_MS,
    ack_timeout_ms: int = _DEFAULT_ACK_TIMEOUT_MS,
) -> bool:
    """Tell the running primary that the extension is alive. Never raises.

    False just means no primary answered, which is the normal case when Cove
    is closed; the native host answers the browser for itself either way.
    """
    return _request(
        name,
        {"version": PROTOCOL_VERSION, "action": EXTENSION_PING_ACTION},
        connect_timeout_ms,
        ack_timeout_ms,
    )


def _request(
    name: str,
    message_obj: dict,
    connect_timeout_ms: int,
    ack_timeout_ms: int,
) -> bool:
    """One bounded request/response round trip against `name`. Never raises.

    Shared by every client-side action so framing, the read deadline, and the
    "only an explicit ok=True counts as success" rule cannot drift apart
    between the magnet path and the browser path.
    """
    sock = QLocalSocket()
    try:
        sock.connectToServer(name)
        if not sock.waitForConnected(connect_timeout_ms):
            return False
        try:
            message = encode_message(message_obj)
        except MessageError:
            return False
        sock.write(message)
        sock.flush()

        deadline = time.monotonic() + ack_timeout_ms / 1000.0
        buf = bytearray()
        while len(buf) < LENGTH_PREFIX_SIZE:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0 or not sock.waitForReadyRead(remaining_ms):
                return False
            buf.extend(bytes(sock.readAll()))
        (length,) = struct.unpack(">I", bytes(buf[:LENGTH_PREFIX_SIZE]))
        if length > MAX_MESSAGE_BYTES:
            return False
        del buf[:LENGTH_PREFIX_SIZE]
        while len(buf) < length:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0 or not sock.waitForReadyRead(remaining_ms):
                return False
            buf.extend(bytes(sock.readAll()))
        try:
            resp = json.loads(bytes(buf[:length]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(resp, dict) and resp.get("ok") is True
    except OSError:
        return False
    finally:
        sock.abort()
