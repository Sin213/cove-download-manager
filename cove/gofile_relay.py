"""Qt-free, ephemeral Gofile capability relay. No request logging or persistence.

aria2's in-memory RPC view and its existing warn-level daemon log can expose
the local capability to the same user; this module cannot hide those surfaces.
"""
from __future__ import annotations

import hmac
import http.client
import itertools
import re
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

from . import gofile

FRESH_AUTHORIZATION = ("Gofile needs fresh authorization from Firefox. Open the Gofile page "
                       "and download the file again.")
TRANSFER_FAILED = "Gofile transfer failed. Download the file again from Firefox."
_CAP = re.compile(r"[A-Za-z0-9_-]{43}")
_RANGE = re.compile(r"bytes=([0-9]{1,19})-([0-9]{0,19})")
_CONTENT_RANGE = re.compile(r"bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19})")


def is_relay_url(url, port=None):
    """Never adopt secret loopback capabilities from aria2's external inventory."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        return parts.hostname in ("127.0.0.1", "::1") and (
            (port is not None and parts.port == port)
            or bool(_CAP.fullmatch(parts.path.split("/")[1])))
    except (ValueError, IndexError, TypeError):
        return False


class _Record:
    def __init__(self, token, url, name, size):
        self.token, self.url, self.name, self.size = token, url, name, size
        self.revoked = threading.Event()
        self.lock, self.responses = threading.Lock(), set()

    def __repr__(self):
        return "<Gofile registration>"

    def revoke(self):
        with self.lock:
            self.revoked.set()
            responses = tuple(self.responses)
            self.responses.clear()
            # Application-level release, not zeroing immutable strings, swap or dumps.
            self.token = self.url = self.name = ""
        for response in responses:
            threading.Thread(target=_close_revoked, args=(response,), daemon=True,
                             name="gofile-revoke").start()


def _close_revoked(response):
    # close() alone can wait behind a trickling read's buffer lock. Interrupt
    # the transport first; neither operation may hold up the UI caller.
    try:
        raw = response.raw
        sock = getattr(getattr(raw, "_connection", None), "sock", None)
        if sock is None:
            buffered = getattr(getattr(raw, "_fp", None), "fp", None)
            sock = getattr(getattr(buffered, "raw", None), "_sock", None)
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        response.close()
    except Exception:
        pass


class _Headers:
    def __init__(self, reader):
        self.reader, self.remaining = reader, 16384

    def readline(self, limit=-1):
        line = self.reader.readline(min(4097, self.remaining + 1))
        self.remaining -= len(line)
        if self.remaining < 0 or len(line) > 4096:
            raise http.client.LineTooLong("headers")
        return line


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, relay):
        self.relay, self.slots = relay, threading.BoundedSemaphore(16)
        self.clients, self.lock = set(), threading.Lock()
        super().__init__(("127.0.0.1", 0), _Handler)

    def process_request(self, request, address):
        request.settimeout(5)
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        with self.lock:
            self.clients.add(request)
        try:
            super().process_request(request, address)
        except Exception:
            self.finish_client(request)
            self.shutdown_request(request)

    def finish_client(self, request):
        with self.lock:
            self.clients.discard(request)
        self.slots.release()

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.finish_client(request)

    def handle_error(self, request, address):
        pass  # Base server exceptions include request state; never print them.


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_error(self, code, *args, **kwargs):
        self._error(code)

    def _error(self, code):
        self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(4097)
            if len(self.raw_requestline) > 4096:
                self.requestline, self.request_version, self.command = "", "HTTP/1.1", ""
                return self._error(414)
            if not self.raw_requestline:
                self.close_connection = True
                return
            reader = self.rfile
            try:
                self.rfile = _Headers(reader)
                parsed = self.parse_request()
            finally:
                self.rfile = reader
            if parsed:
                if self.raw_requestline.split()[1].decode("iso-8859-1") != self.path:
                    return self._error(404)
                self._serve()
                self.wfile.flush()
        except (OSError, ValueError):
            self.close_connection = True

    def handle_expect_100(self):
        self._error(400)
        return False

    def _serve(self):
        self.close_connection = True
        if self.command not in ("GET", "HEAD"):
            return self._error(405)
        host = self.headers.get_all("Host", [])
        ranges = self.headers.get_all("Range", [])
        lengths = self.headers.get_all("Content-Length", [])
        if (len(host) != 1 or len(ranges) > 1 or len(lengths) > 1
                or self.headers.get_all("Transfer-Encoding")
                or (lengths and lengths != ["0"])):
            return self._error(400)
        if (host != [f"127.0.0.1:{self.server.server_port}"] or "Origin" in self.headers
                or any(key.lower().startswith("sec-fetch-") for key in self.headers)):
            return self._error(403)
        record = self.server.relay._lookup(self.path)
        if record is None:
            return self._error(404)
        match = _RANGE.fullmatch(ranges[0]) if ranges else None
        if ranges and (not match or int(match[1]) > 2**62
                       or (match[2] and not int(match[1]) <= int(match[2]) <= 2**62)):
            return self._error(416)
        start = int(match[1]) if match else 0
        session = response = None
        sent_headers = False
        try:
            with record.lock:
                if record.revoked.is_set() or not gofile.is_delivery_url(record.url):
                    return self._error(404)
                url = gofile._download_url(record.url)
                headers = {"Cookie": f"accountToken={record.token}", "Accept-Encoding": "identity"}
            if ranges:
                headers["Range"] = ranges[0]
            session = self.server.relay.session_factory()
            response = session.get(url, headers=headers, stream=True,
                                   allow_redirects=False, timeout=(3, 5))
            with record.lock:
                if record.revoked.is_set():
                    return self._error(404)
                record.responses.add(response)
            status = response.status_code
            if status not in (200, 206):
                return self._error({401: 403, 403: 403, 404: 404, 410: 404,
                                    416: 416, 429: 429}.get(status, 502))
            length = response.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,19}", length) or not 0 < int(length) <= 2**62:
                return self._error(502)
            length = int(length)
            if status == 206:
                content_range = _CONTENT_RANGE.fullmatch(response.headers.get("Content-Range", ""))
                if not content_range:
                    return self._error(502)
                first, last, total = map(int, content_range.groups())
                if (first != start or not 0 <= first <= last < total <= 2**62
                        or length != last - first + 1 or (record.size and total != record.size)
                        or (match and match[2] and last > int(match[2]))):
                    return self._error(502)
                # A short prefix is valid only for an explicit, exact small range;
                # unsolicited/shortened offset-zero responses cannot hide the file head.
                if (start == 0 and length < min(4096, total)
                        and (not match or not match[2] or int(match[2]) + 1 != length)):
                    return self._error(502)
            elif start or (record.size and length != record.size):
                return self._error(502)
            forwarded = {}
            for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                value = response.headers.get(key)
                if value is not None:
                    if len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                        return self._error(502)
                    forwarded[key] = value
            content_type = forwarded.get("Content-Type", "").lower()
            if (content_type.startswith("text/html")
                    or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
                return self._error(502)
            chunks = iter(response.iter_content(4096))
            prefix = bytearray()
            pending = b""
            if start == 0:
                while len(prefix) < min(4096, length):
                    chunk = next(chunks, b"")
                    if not chunk:
                        break
                    remaining = min(4096, length) - len(prefix)
                    prefix.extend(chunk[:remaining])
                    pending = chunk[remaining:]
                if (not prefix or len(prefix) < min(4096, length)
                        or gofile.looks_like_web_page(bytes(prefix))
                        or re.search(rb'"error"\s*:|"status"\s*:\s*"error', prefix)):
                    return self._error(502)
            if record.revoked.is_set():
                return self._error(404)
            self.send_response(status)
            for key, value in forwarded.items():
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            sent_headers = True
            if self.command == "HEAD":
                return
            sent = len(prefix)
            self.wfile.write(prefix)
            for chunk in itertools.chain((pending,), chunks):
                if record.revoked.is_set() or sent + len(chunk) > length:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
            # Always close: a short Content-Length body must fail at aria2.
        except Exception:
            if not sent_headers:
                self._error(502)
        finally:
            if response is not None:
                with record.lock:
                    record.responses.discard(response)
                response.close()
            if session is not None:
                session.close()


class GofileRelay:
    def __init__(self, settings, *, session_factory=None):
        self.session_factory = session_factory or (lambda: gofile.upstream_session(settings))
        self._lock, self._records = threading.RLock(), {}
        self._server = self._thread = None

    def start(self):
        with self._lock:
            if self._server is None:
                server = _Server(self)
                self._server = server
                self._thread = threading.Thread(target=server.serve_forever, daemon=True,
                                                name="gofile-relay")
                self._thread.start()

    def register(self, url, token, filename, size):
        if (not gofile.is_delivery_url(url) or not isinstance(token, str)
                or not gofile._TOKEN_RE.fullmatch(token) or not isinstance(filename, str)
                or not filename or filename in (".", "..") or len(filename) > 255
                or any(c in filename for c in "/\\%") or any(ord(c) < 32 for c in filename)
                or isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 2**62):
            raise ValueError(TRANSFER_FAILED)
        with self._lock:
            self.start()
            capability = secrets.token_urlsafe(32)
            self._records[capability] = _Record(token, url, quote(filename, safe=""), size)
            return capability

    def local_url(self, capability):
        with self._lock:
            record = self._records.get(capability)
            if record is None or self._server is None:
                raise ValueError(FRESH_AUTHORIZATION)
            return f"http://127.0.0.1:{self._server.server_port}/{capability}/{record.name}"

    def _lookup(self, path):
        parts = path.split("/")
        if len(parts) != 3 or parts[0] or not _CAP.fullmatch(parts[1]):
            return None
        with self._lock:
            for capability, record in self._records.items():
                if hmac.compare_digest(capability, parts[1]) and parts[2] == record.name:
                    return record
        return None

    def unregister(self, capability):
        with self._lock:
            record = self._records.pop(capability, None)
        if record is not None:
            record.revoke()

    def stop(self):
        with self._lock:
            records, self._records = list(self._records.values()), {}
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
        for record in records:
            record.revoke()
        if server is not None:
            with server.lock:
                for client in tuple(server.clients):
                    try:
                        client.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            server.shutdown()
            server.server_close()
            thread.join(timeout=.1)
