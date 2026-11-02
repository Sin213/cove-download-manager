"""Bounded Gofile relay proofs; no real Gofile hosts or credentials."""
import io
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from cove import gofile, gofile_relay as relay_module, netiface
from cove.config import Settings
from tests.test_gofile import Resp, TOKEN

URL = "https://store-1.gofile.io/download/web/11111111-aaaa-4aaa-8aaa-111111111111/movie.mp4"


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        pass


@pytest.mark.parametrize("browser", [False, True])
def test_browser_probe_redirect_policy_keeps_legacy_working(browser):
    session = Session(Resp(status=302, headers={"Location": URL}),
                      Resp(headers={"Content-Length": "5"}, body=b"video"))
    def probe():
        return gofile.verify_browser_download(URL, "movie.mp4", Settings(gofile_api_token=TOKEN),
            session=session, deadline=time.monotonic() + 1, **({"browser_token": TOKEN} if browser else {}))
    if browser:
        with pytest.raises(gofile.GofileError): probe()
        assert len(session.calls) == 1
    else:
        assert probe() == 5 and len(session.calls) == 2


@pytest.mark.parametrize("browser,status,length,total,body,accepted", [
    (True, 206, 1, 20, b"<", False), (False, 206, 1, 20, b"<", True),
    (True, 206, 4096, 5000, b"v" * 4096, True), (True, 200, 5000, 5000, b"v" * 4096, True)])
def test_probe_requires_complete_browser_prefix(browser, status, length, total, body, accepted):
    headers = {"Content-Length": str(length), "Content-Range": f"bytes 0-{length - 1}/{total}"}
    def probe():
        return gofile.verify_browser_download(URL, "movie.mp4", Settings(gofile_api_token=TOKEN),
            session=Session(Resp(status=status, headers=headers, body=body)), deadline=time.monotonic() + 1,
            **({"browser_token": TOKEN} if browser else {}))
    if accepted: assert probe() == total
    else:
        with pytest.raises(gofile.GofileError) as error: probe()
        assert error.value.code == "gofile_unavailable"


@pytest.mark.parametrize("ranged,code", [(None, 502), ("bytes=0-0", 206), ("bytes=0-19", 502)])
def test_relay_short_prefix_requires_explicit_short_range(relay, ranged, code):
    relay.relay._records[relay.cap].size = 20
    relay.session.responses = [Response(b"<", status=206, headers={"Content-Range": "bytes 0-0/20"})]
    assert request(relay, headers={"Range": ranged} if ranged else {})[0] == code


def test_revoke_does_not_wait_for_blocking_response_close(relay):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    class Blocking(Response):
        def close(self):
            entered.set()
            release.wait(1.2)
            super().close()
            finished.set()
    record = relay.relay._records[relay.cap]
    record.responses.add(Blocking())
    try:
        before = time.monotonic()
        relay.relay.unregister(relay.cap)
        assert time.monotonic() - before < .3
        assert record.revoked.is_set() and not record.token and entered.wait(.2)
    finally:
        release.set()
        assert finished.wait(1)


class Response(Resp):
    def __init__(self, body=b"video", *, status=200, headers=None, fragments=None):
        super().__init__(status=status, body=body, headers={
            "Content-Length": str(len(body)), "Content-Type": "video/mp4", **(headers or {})})
        self.fragments = fragments

    def iter_content(self, size):
        yield from self.fragments if self.fragments is not None else (
            self._body[n:n + size] for n in range(0, len(self._body), size))


class Connection:
    """Real HTTP handler with in-memory socket I/O, usable in socket sandboxes."""
    def __init__(self, request):
        self.reader, self.output = io.BytesIO(request), bytearray()

    def makefile(self, *args):
        return self.reader

    def sendall(self, data):
        self.output.extend(data)


@pytest.fixture
def relay(monkeypatch):
    session = Session(Response())
    instance = relay_module.GofileRelay(Settings(), session_factory=lambda: session)
    instance._server = SimpleNamespace(server_port=12345)
    monkeypatch.setattr(instance, "start", lambda: None)
    cap = instance.register(URL, TOKEN, "movie.mp4", 5)
    yield SimpleNamespace(relay=instance, session=session, cap=cap,
                          path=urlsplit(instance.local_url(cap)).path)
    instance.unregister(cap)


def request(state, path=None, *, method="GET", headers=None, raw_headers=None):
    headers = {"Host": "127.0.0.1:12345", **(headers or {})}
    lines = [f"{method} {state.path if path is None else path} HTTP/1.1",
             *(f"{key}: {value}" for key, value in headers.items()), *(raw_headers or []), "", ""]
    connection = Connection("\r\n".join(lines).encode())
    relay_module._Handler(connection, ("127.0.0.1", 1),
                          SimpleNamespace(relay=state.relay, server_port=12345))
    head, body = bytes(connection.output).split(b"\r\n\r\n", 1)
    return int(head.split()[1]), head, body


def test_capability_only_and_exact_upstream_headers(relay, capsys):
    code, head, body = request(relay, headers={"Authorization": "discard", "Cookie": "discard",
                                              "User-Agent": "discard", "X-URL": "https://evil.test"})
    assert code == 200 and body == b"video"
    assert relay.session.calls == [(URL, {"headers": {"Cookie": f"accountToken={TOKEN}",
        "Accept-Encoding": "identity"}, "stream": True, "allow_redirects": False, "timeout": (3, 5)})]
    assert TOKEN.encode() not in head and URL.encode() not in head and relay.cap.encode() not in head
    assert not capsys.readouterr().out and not capsys.readouterr().err


@pytest.mark.parametrize("path", ["/", "/movie.mp4", "/{wrong}/movie.mp4", "/{cap}/other.mp4",
    "/{cap}/../movie.mp4", "/{cap}/%2fmovie.mp4", "/{cap}//movie.mp4", "//{cap}/movie.mp4",
    "/{cap}/movie.mp4/extra", "/{cap}/movie.mp4?url=https://evil.test", "/{cap}/movie.mp4#x",
    "http://evil.test/{cap}/movie.mp4", "/{cap}x/movie.mp4", "/{cap}/%2e%2e"])
def test_malformed_or_unknown_capabilities_do_not_fetch(relay, path):
    path = path.format(cap=relay.cap, wrong="x" * 43)
    assert request(relay, path)[0] == 404
    assert not relay.session.calls


@pytest.mark.parametrize("headers,raw,code", [
    ({"Host": "localhost:12345"}, [], 403), ({"Host": "evil.test"}, [], 403),
    ({"Host": "127.0.0.1:1"}, [], 403), ({"Origin": ""}, [], 403),
    ({"Sec-Fetch-Site": "none"}, [], 403), ({"Sec-Fetch-Foo": ""}, [], 403),
    ({}, ["Host: 127.0.0.1:12345"], 400),
    ({"Range": "bytes=0-4"}, ["Range: bytes=0-4"], 400),
    ({"Content-Length": "1"}, [], 400), ({"Content-Length": "0"}, ["Content-Length: 0"], 400),
    ({"Transfer-Encoding": "chunked"}, [], 400),
    ({"Expect": "100-continue", "Content-Length": "1"}, [], 400),
])
def test_browser_and_framing_rejections(relay, headers, raw, code):
    assert request(relay, headers=headers, raw_headers=raw)[0] == code
    assert not relay.session.calls


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS", "PATCH", "CONNECT", "TRACE"])
def test_methods(relay, method):
    assert request(relay, method=method)[0] == 405
    assert not relay.session.calls


@pytest.mark.parametrize("value", ["bytes=-2", "bytes=4-2", "bytes=0-1,3-4", "items=0-1",
                                     "bytes=", "bytes=0-" + "9" * 20, "bytes=9999999999999999999-"])
def test_ranges_fail_closed(relay, value):
    assert request(relay, headers={"Range": value})[0] == 416
    assert not relay.session.calls


def test_range_and_head(relay):
    relay.session.responses = [Response(b"deo", status=206, headers={"Content-Range": "bytes 2-4/5"})]
    assert request(relay, headers={"Range": "bytes=2-4"})[::2] == (206, b"deo")
    assert relay.session.calls[0][1]["headers"]["Range"] == "bytes=2-4"
    relay.session.responses = [Response()]
    code, head, body = request(relay, method="HEAD")
    assert code == 200 and body == b"" and b"Content-Length: 5" in head


@pytest.mark.parametrize("status,code", [(301, 502), (302, 502), (303, 502), (304, 502),
    (307, 502), (308, 502), (401, 403), (403, 403), (404, 404), (410, 404), (416, 416),
    (429, 429), (500, 502)])
def test_status_mapping_does_not_forward_location_or_body(relay, status, code):
    response = Response(TOKEN.encode(), status=status, headers={"Location": URL})
    relay.session.responses = [response]
    actual, head, body = request(relay)
    assert actual == code and not body and b"Location" not in head
    assert TOKEN.encode() not in head and response.closed and len(relay.session.calls) == 1


@pytest.mark.parametrize("body,ctype", [(b"<html>login</html>", "video/mp4"),
    (b"<!doctype html>login", "video/mp4"), (b'{"status":"error-token"}', "application/json"),
    (b'{"error":true}', "application/json"), (b'{"error":"unauthorized"}', "application/json"),
    (b"login", "text/html"), (b"", "video/mp4")])
def test_fragmented_pages_and_empty_files_never_send_success(relay, body, ctype):
    record = relay.relay._records[relay.cap]
    record.size = len(body)
    record.name = "page.html"  # Filename never exempts browser-delivery HTML.
    relay.path = relay.path.rsplit("/", 1)[0] + "/page.html"
    relay.session.responses = [Response(body, fragments=[bytes([c]) for c in body],
                                        headers={"Content-Type": ctype})]
    assert request(relay)[0] == 502


@pytest.mark.parametrize("status,range_header,extra", [
    (206, "bytes 1-3/5", {}), (206, "bytes 2-4/6", {}), (206, "bytes 2-4/5", {"Content-Length": "4"}),
    (206, "bytes */5", {}), (200, "", {}), (206, "bytes 2-3/5", {}),
])
def test_range_mismatch_is_rejected(relay, status, range_header, extra):
    relay.session.responses = [Response(b"deo", status=status,
        headers={"Content-Range": range_header, **extra})]
    assert request(relay, headers={"Range": "bytes=2-4"})[0] == 502


def test_truncation_after_valid_prefix_is_short_not_complete(relay):
    record = relay.relay._records[relay.cap]
    record.size = 8192
    relay.session.responses = [Response(b"x" * 4096, headers={"Content-Length": "8192"})]
    code, head, body = request(relay)
    assert code == 200 and len(body) == 4096 and b"Content-Length: 8192" in head


def test_fragmented_prefix_crossing_boundary_streams_without_loss(relay):
    body = b"v" * 5000
    relay.relay._records[relay.cap].size = len(body)
    relay.session.responses = [Response(body, fragments=[body[:4095], body[4095:]])]
    assert request(relay)[::2] == (200, body)


def test_revalidation_and_revocation_release_application_references(relay):
    record = relay.relay._records[relay.cap]
    record.url = "https://evil.test/file"
    assert request(relay)[0] == 404 and not relay.session.calls
    response = Response()
    record.responses.add(response)
    relay.relay.unregister(relay.cap)
    deadline = time.monotonic() + .5
    while not response.closed and time.monotonic() < deadline: time.sleep(.001)
    assert response.closed and record.revoked.is_set() and not record.responses
    assert not record.token and not record.url and not record.name
    assert request(relay)[0] == 404
    with pytest.raises(ValueError, match="fresh authorization"):
        relay.relay.local_url(relay.cap)
    assert TOKEN not in repr(record) and URL not in repr(record)


def test_client_abort_closes_upstream(relay, monkeypatch):
    response = Response()
    relay.session.responses = [response]
    def abort(*args):
        raise BrokenPipeError()
    monkeypatch.setattr(Connection, "sendall", abort)
    connection = Connection(f"GET {relay.path} HTTP/1.1\r\nHost: 127.0.0.1:12345\r\n\r\n".encode())
    relay_module._Handler(connection, ("127.0.0.1", 1),
                          SimpleNamespace(relay=relay.relay, server_port=12345))
    assert response.closed


def test_header_and_request_line_caps(relay):
    assert request(relay, headers={"X-Padding": "a" * 4097})[0] == 431
    assert request(relay, "/" + "a" * 4097)[0] == 414
    assert request(relay, raw_headers=[f"X-{i}: " + "a" * 2000 for i in range(9)])[0] == 431
    assert not relay.session.calls


def test_server_binds_numeric_address_and_limits_connections(monkeypatch):
    seen = []
    monkeypatch.setattr(relay_module.ThreadingHTTPServer, "__init__",
                        lambda self, address, handler: seen.append(address))
    server = relay_module._Server(object())
    assert seen == [("127.0.0.1", 0)]
    assert all(server.slots.acquire(blocking=False) for _ in range(16))
    assert not server.slots.acquire(blocking=False)
    for _ in range(16):
        server.slots.release()


def test_excess_connection_gets_immediate_fixed_503(monkeypatch):
    server = object.__new__(relay_module._Server)
    server.slots = threading.BoundedSemaphore(1)
    server.slots.acquire()
    closed, timeouts, output = [], [], []
    server.shutdown_request = lambda sock: closed.append(sock)
    connection = SimpleNamespace(settimeout=lambda timeout: timeouts.append(timeout),
                                 sendall=lambda data: output.append(data))
    server.process_request(connection, ("127.0.0.1", 1))
    assert timeouts == [5] and closed == [connection]
    assert output == [b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"]


@pytest.mark.parametrize("value", [URL.replace("https:", "http:"), URL + "?x", URL + "#x",
    URL.replace(".io/", ".io:444/"), URL.replace("https://", "https://u@"),
    URL.replace(".io/", ".io\\evil/"), URL.replace("gofile", "gofıle"),
    URL.replace("store-1", "%73tore-1"), URL.replace(".io/", ".io.evil/"),
    URL.replace("store-1", "xn--store-9za")])
def test_registration_never_authorizes_unsafe_upstream(relay, value):
    with pytest.raises(ValueError) as error:
        relay.relay.register(value, TOKEN, "movie.mp4", 5)
    assert TOKEN not in str(error.value) and value not in str(error.value)
    assert len(relay.relay._records) == 1


@pytest.mark.parametrize("name", [".", "..", "a/b", "a\\b", "%2f", "a\n", "", "a" * 256])
def test_registration_rejects_unsafe_names(relay, name):
    with pytest.raises(ValueError):
        relay.relay.register(URL, TOKEN, name, 5)


def test_upstream_session_uses_explicit_proxy_over_environment(monkeypatch):
    import requests
    seen = []
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong.test:1")
    monkeypatch.setattr(netiface, "bound_requests_session",
                        lambda interface: seen.append(interface) or requests.Session())
    settings = Settings(proxy_type="http", proxy_host="proxy.test", proxy_port=3128,
                        proxy_username="a@b", proxy_password="p/q", torrent_network_interface="chosen")
    session = gofile.upstream_session(settings)
    try:
        merged = session.merge_environment_settings(URL, {}, False, None, None)
        assert merged["proxies"]["https"] == "http://a%40b:p%2Fq@proxy.test:3128"
        assert merged["verify"] is True and seen == ["chosen"] and not session.trust_env
    finally:
        session.close()


def test_no_proxy_preserves_existing_session_policy(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(netiface, "bound_requests_session", lambda _: sentinel)
    assert gofile.upstream_session(Settings()) is sentinel


def test_socks_missing_refuses_before_creating_session(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "socks", None)
    monkeypatch.setattr(netiface, "bound_requests_session", lambda _: pytest.fail("session created"))
    with pytest.raises(gofile.GofileError):
        gofile.upstream_session(Settings(proxy_type="socks5"))


def test_upstream_timeout_is_fixed_and_releases_response(relay):
    class Hanging(Response):
        def iter_content(self, size):
            raise TimeoutError(TOKEN + URL)
    response = Hanging()
    relay.session.responses = [response]
    code, head, body = request(relay)
    assert code == 502 and not body and response.closed
    assert TOKEN.encode() not in head and URL.encode() not in head


def test_revocation_aborts_an_inflight_stream(relay):
    record = relay.relay._records[relay.cap]
    record.size = 8192
    class Revoked(Response):
        def iter_content(self, size):
            yield b"v" * 4096
            relay.relay.unregister(relay.cap)
            yield b"v" * 4096
    response = Revoked(headers={"Content-Length": "8192"})
    relay.session.responses = [response]
    assert request(relay)[::2] == (200, b"v" * 4096)
    assert response.closed and record.revoked.is_set() and not record.token


def test_new_add_uri_option_is_optional_and_cookie_free():
    from cove.aria2 import Aria2RPC
    rpc = object.__new__(Aria2RPC)
    calls = []
    rpc._call = lambda *args: calls.append(args) or "gid"
    rpc.add_uri(["http://127.0.0.1:1/file"], "/tmp", 2)
    old = calls[-1][1][1].copy()
    rpc.add_uri(["http://127.0.0.1:1/file"], "/tmp", 2, options={"no-proxy": "127.0.0.1"})
    assert calls[-1][1][1] == {**old, "no-proxy": "127.0.0.1"}
    assert "header" not in old and "Cookie" not in repr(calls)


@pytest.fixture
def loopback_storage(tmp_path):
    """Small cookie-gated storage on a different port, never real Gofile."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import requests
    body = b"v" * 32768
    state = SimpleNamespace(body=body, mode="good", calls=[], total=0,
                            started=threading.Event(), release=threading.Event())
    class Storage(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            state.calls.append((self.path, dict(self.headers)))
            if state.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if (state.mode == "revoked" or self.headers.get("Cookie") != f"accountToken={TOKEN}"
                    or self.path != "/file"):
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            match = relay_module._RANGE.fullmatch(self.headers.get("Range", "bytes=0-"))
            assert match
            start = int(match[1])
            end = min(int(match[2]) if match[2] else len(body) - 1, len(body) - 1,
                      start + 256 * 1024 - 1)  # Range clamp AND hard stream cut-off.
            payload = body[start:end + 1][:256 * 1024]
            state.total += len(payload)
            assert state.total <= 256 * 1024
            self.send_response(206 if start or end != len(body) - 1 else 200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "video/mp4")
            if start or end != len(body) - 1:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.end_headers()
            if state.mode == "trickle":
                self.wfile.write(payload[:8192])
                self.wfile.flush()
                state.started.set()
                for _ in range(100):
                    if state.release.wait(.1): break
                    try:
                        self.wfile.write(b"v")
                        self.wfile.flush()
                    except OSError:
                        break
                self.close_connection = True
                return
            if state.mode == "slow":
                self.wfile.write(payload[:8192])
                self.wfile.flush()
                state.started.set()
                state.release.wait(8)
                if state.mode == "revoked":
                    self.close_connection = True
                    return
                payload = payload[8192:]
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Storage)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    class MappedSession:
        def __init__(self):
            self.session = requests.Session()
            self.session.trust_env = False

        def get(self, url, **kwargs):
            assert url == URL
            return self.session.get(f"http://127.0.0.1:{server.server_port}/file", **kwargs)

        def close(self):
            self.session.close()
    relay = relay_module.GofileRelay(Settings(), session_factory=MappedSession)
    try:
        state.relay = relay
        state.cap = relay.register(URL, TOKEN, "movie.mp4", len(body))
        state.url = relay.local_url(state.cap)
        assert urlsplit(state.url).port != server.server_port
        assert relay._server.server_address[0] == "127.0.0.1"
        yield state
    finally:
        state.release.set()
        relay.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_loopback_concurrent_segments_and_unregister(loopback_storage):
    import concurrent.futures
    import requests
    state = loopback_storage
    def segment(start):
        with requests.Session() as session:
            session.trust_env = False
            with session.get(state.url, headers={"Range": f"bytes={start}-{start + 8191}"},
                             stream=True, timeout=8) as response:
                assert response.status_code == 206
                result = bytearray()
                for chunk in response.iter_content(4096):
                    result.extend(chunk)
                    assert len(result) <= 8192
                return bytes(result)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(segment, (0, 8192, 16384, 24576)))
    assert b"".join(result) == state.body
    state.relay.unregister(state.cap)
    with requests.Session() as session:
        session.trust_env = False
        with session.get(state.url, timeout=3) as response:
            assert response.status_code == 404


def test_loopback_slow_headers_and_connection_cap(loopback_storage):
    state = loopback_storage
    address = ("127.0.0.1", urlsplit(state.url).port)
    sockets = []
    try:
        for _ in range(16):
            sock = socket.create_connection(address, timeout=2)
            sockets.append(sock)
            sock.sendall(b"GET / HTTP/1.1\r\nX-Slow: ")
        deadline = time.monotonic() + 2
        while len(state.relay._server.clients) < 16 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(state.relay._server.clients) == 16
        with socket.create_connection(address, timeout=2) as extra:
            assert b"503" in extra.recv(256)
        sockets[0].settimeout(7)
        assert sockets[0].recv(256) == b""  # Header timeout is five seconds.
    finally:
        for sock in sockets:
            sock.close()


@pytest.mark.parametrize("operation", ["unregister", "stop"])
def test_loopback_trickling_upstream_revocation_is_prompt(loopback_storage, operation):
    import requests
    state = loopback_storage
    state.mode = "trickle"
    server = state.relay._server
    with requests.Session() as session:
        session.trust_env = False
        with session.get(state.url, stream=True, timeout=2) as response:
            chunks = response.iter_content(4096)
            assert len(next(chunks)) == len(next(chunks)) == 4096 and state.started.wait(1)
            before = time.monotonic()
            if operation == "stop": state.relay.stop()
            else: state.relay.unregister(state.cap)
            assert time.monotonic() - before < 1
            with pytest.raises(requests.RequestException):
                received = 8192
                for chunk in chunks:
                    received += len(chunk)
                    assert received <= 256 * 1024
            deadline = time.monotonic() + 1
            while server.clients and time.monotonic() < deadline: time.sleep(.005)
            assert not server.clients


@pytest.mark.parametrize("redirect", [False, True])
def test_loopback_real_aria2_transfer_with_dead_proxy(loopback_storage, tmp_path, monkeypatch, redirect):
    import shutil
    from cove import aria2
    if shutil.which("aria2c") is None:
        pytest.skip("real aria2c is not installed")
    state = loopback_storage
    state.mode = "redirect" if redirect else "good"
    data = tmp_path / "engine"
    data.mkdir()
    monkeypatch.setattr(aria2, "DATA_DIR", data)
    monkeypatch.setattr(aria2, "ARIA2_LOG", data / "aria2.log")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    settings = Settings(rpc_port=port, proxy_type="http", proxy_host="127.0.0.1", proxy_port=1,
                        download_dir=str(tmp_path / "out"))
    daemon = aria2.Aria2Daemon(settings)
    rpc = None
    try:
        daemon.start()
        rpc = aria2.Aria2RPC(settings, timeout=2, secret_provider=daemon.active_rpc_secret)
        gid = rpc.add_uri([state.url], settings.download_dir, 2, filename="movie.mp4",
                          options={"no-proxy": "127.0.0.1"})
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            result = rpc.tell_status(gid)
            if result["status"] in ("complete", "error"):
                break
            time.sleep(.05)
        assert result["status"] == ("error" if redirect else "complete")
        assert state.calls and all(path == "/file" for path, _ in state.calls)
        assert all(headers.get("Cookie") == f"accountToken={TOKEN}" for _, headers in state.calls)
        options = rpc._call("aria2.getOption", [gid])
        assert TOKEN not in str(options) and "Cookie" not in str(options.get("header", ""))
        if not redirect:
            assert (tmp_path / "out" / "movie.mp4").read_bytes() == state.body
    finally:
        if rpc is not None:
            rpc.close()
        daemon.stop()
