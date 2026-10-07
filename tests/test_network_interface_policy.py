"""Requests routing policy: synthetic loopback targets, proxies, and credentials.

No external endpoints or probe imports. Permission errors are test failures;
only a host missing the selected loopback alias may skip socket tests.
"""
import base64
from contextlib import contextmanager, ExitStack
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from pathlib import Path
import re
import select
import shutil
import socket
import ssl
import subprocess
import threading
from types import SimpleNamespace

import pytest
import requests

from cove import netiface


SOURCE_A = "127.0.0.2"
SOURCE_B = "127.0.0.3"
ENV_KEYS = tuple(
    key
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
    for key in (name, name.lower())
) + ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NETRC", "SSL_CERT_FILE", "SSL_CERT_DIR")


@pytest.fixture(autouse=True)
def clean_requests_environment(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Prevent a developer's ~/.netrc from supplying unrelated credentials.
    monkeypatch.setenv("NETRC", str(tmp_path / "no-netrc"))


def _check_loopback_alias(address):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((address, 0))
        except OSError as exc:
            if address == SOURCE_A and exc.errno == errno.EADDRNOTAVAIL:
                pytest.skip("host cannot bind the 127.0.0.2 loopback alias")
            raise


class _RecordingServer(ThreadingHTTPServer):
    # server_close joins every handler; the serving thread is joined below.
    daemon_threads = False
    block_on_close = True

    def __init__(self, handler):
        self.peers = []
        self.requests = []
        self.authorizations = []
        super().__init__(("127.0.0.1", 0), handler)

    def get_request(self):
        connection, peer = super().get_request()
        connection.settimeout(5)
        self.peers.append(peer[0])
        return connection, peer

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_port}"


class _TargetHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self):
        self.server.requests.append((self.path, self.client_address[0]))
        self._reply(200, b"target-ok")


class _ProxyHandler(_TargetHandler):
    def _authenticated(self):
        supplied = self.headers.get("Proxy-Authorization")
        self.server.authorizations.append(supplied)
        expected = self.server.expected_authorization
        if expected is not None and supplied != expected:
            self._reply(407, b"proxy-auth-required")
            return False
        return True

    def do_GET(self):
        if self._authenticated():
            self.server.requests.append((self.path, self.client_address[0]))
            self._reply(200, b"via-proxy")

    def do_CONNECT(self):
        if not self._authenticated():
            return
        self.server.requests.append((self.path, self.client_address[0]))
        # CONNECT is permitted only to our other local random-port fixture.
        assert self.path == self.server.connect_target
        host, port = self.path.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=5) as upstream:
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            while True:
                readable, _, _ = select.select([self.connection, upstream], [], [], 5)
                if not readable:
                    return
                for incoming in readable:
                    data = incoming.recv(65536)
                    if not data:
                        return
                    outgoing = upstream if incoming is self.connection else self.connection
                    outgoing.sendall(data)


@contextmanager
def _serve(handler, *, tls_context=None, auth=False, connect_target=None):
    server = _RecordingServer(handler)
    try:
        server.expected_authorization = (
            "Basic " + base64.b64encode(b"test-user:test-pass").decode("ascii")
            if auth else None
        )
        server.connect_target = connect_target
        if tls_context is not None:
            server.socket = tls_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=10)
            assert not thread.is_alive(), "loopback server did not stop"
    finally:
        server.server_close()


@pytest.fixture
def loopback_alias():
    _check_loopback_alias(SOURCE_A)


@pytest.fixture
def loopback_servers(loopback_alias):
    with ExitStack() as stack:
        target = stack.enter_context(_serve(_TargetHandler))
        proxy = stack.enter_context(_serve(_ProxyHandler))
        yield target, proxy


@pytest.fixture
def tls_certificate(tmp_path):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is unavailable for the throwaway TLS certificate")
    certificate = tmp_path / "certificate.pem"
    private_key = tmp_path / "private-key.pem"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(private_key), "-out", str(certificate), "-days", "2",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, private_key)
    return certificate, context


@pytest.fixture
def selected_interface(monkeypatch):
    monkeypatch.setattr(netiface, "interface_address", lambda name: SOURCE_A)


def _get(session, url, expected_body):
    with session.get(url, timeout=5, allow_redirects=False) as response:
        assert response.status_code == 200
        assert response.text == expected_body


def test_n1_direct_http_uses_selected_source(loopback_servers, selected_interface):
    target, proxy = loopback_servers
    with netiface.bound_requests_session("synthetic-vpn") as session:
        _get(session, target.url + "/direct", "target-ok")
    assert target.peers == [SOURCE_A]
    assert proxy.peers == []


def test_n2_http_proxy_uses_selected_source(
    loopback_servers, selected_interface, monkeypatch,
):
    target, proxy = loopback_servers
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    with netiface.bound_requests_session("synthetic-vpn") as session:
        _get(session, target.url + "/proxy", "via-proxy")
    assert proxy.peers == [SOURCE_A], "HTTP_PROXY bypassed the selected source address"
    assert target.peers == []


def test_n3_https_env_proxy_manager_receives_source_address(selected_interface, monkeypatch):
    proxy = "http://127.0.0.1:1080"
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    url = "https://127.0.0.1/"
    with netiface.bound_requests_session("synthetic-vpn") as session:
        # Use Requests' real environment merge and routing choice. Constructing
        # the effective manager opens no socket, so this check also runs in a
        # sandbox that cannot execute the real CONNECT companion test.
        settings = session.merge_environment_settings(url, {}, None, None, None)
        selected_proxy = requests.utils.select_proxy(url, settings["proxies"])
        assert selected_proxy == proxy
        manager = session.get_adapter(url).proxy_manager_for(selected_proxy)
        assert manager.connection_pool_kw.get("source_address") == (SOURCE_A, 0), (
            "the effective HTTPS proxy manager has no selected source address"
        )


def test_n3_https_proxy_manager_and_connect_use_selected_source(
    loopback_alias, selected_interface, tls_certificate, monkeypatch,
):
    certificate, context = tls_certificate
    with _serve(_TargetHandler, tls_context=context) as target, _serve(
        _ProxyHandler, connect_target=f"127.0.0.1:{target.server_port}",
    ) as proxy:
        monkeypatch.setenv("HTTPS_PROXY", proxy.url)
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(certificate))
        url = f"https://127.0.0.1:{target.server_port}/tls"
        with netiface.bound_requests_session("synthetic-vpn") as session:
            _get(session, url, "target-ok")
            manager = session.get_adapter(url).proxy_manager[proxy.url]
            assert manager.connection_pool_kw.get("source_address") == (SOURCE_A, 0), (
                "the effective HTTPS proxy manager has no selected source address"
            )
        assert proxy.peers == [SOURCE_A], "CONNECT used the default interface"
        assert len(target.requests) == 1


def test_n4_all_proxy_uses_selected_source(
    loopback_servers, selected_interface, monkeypatch,
):
    target, proxy = loopback_servers
    monkeypatch.setenv("ALL_PROXY", proxy.url)
    with netiface.bound_requests_session("synthetic-vpn") as session:
        _get(session, target.url + "/all-proxy", "via-proxy")
    assert proxy.peers == [SOURCE_A], "ALL_PROXY bypassed the selected source address"
    assert target.peers == []


def test_n5_no_proxy_bypasses_proxy_and_keeps_binding(
    loopback_servers, selected_interface, monkeypatch,
):
    target, proxy = loopback_servers
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with netiface.bound_requests_session("synthetic-vpn") as session:
        _get(session, target.url + "/bypass", "target-ok")
    assert proxy.peers == []
    assert proxy.requests == []
    assert target.peers == [SOURCE_A]


def test_n6_authenticated_proxy_authenticates_and_binds(
    loopback_servers, selected_interface, monkeypatch,
):
    target, _ = loopback_servers
    with _serve(_ProxyHandler, auth=True) as proxy:
        monkeypatch.setenv(
            "HTTP_PROXY", f"http://test-user:test-pass@127.0.0.1:{proxy.server_port}",
        )
        with netiface.bound_requests_session("synthetic-vpn") as session:
            _get(session, target.url + "/authenticated", "via-proxy")
        assert proxy.authorizations == [proxy.expected_authorization]
        assert proxy.peers == [SOURCE_A], "authenticated proxy bypassed source binding"
    assert target.peers == []


def test_n7_same_proxy_interleaved_sessions_keep_their_own_source(
    loopback_servers, monkeypatch,
):
    target, proxy = loopback_servers
    _check_loopback_alias(SOURCE_B)
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    addresses = {"vpn-a": SOURCE_A, "vpn-b": SOURCE_B}
    monkeypatch.setattr(netiface, "interface_address", addresses.get)
    with netiface.bound_requests_session("vpn-a") as session_a, \
            netiface.bound_requests_session("vpn-b") as session_b:
        for session, path in (
            (session_a, "/a1"), (session_b, "/b1"),
            (session_a, "/a2"), (session_b, "/b2"),
        ):
            _get(session, target.url + path, "via-proxy")
        observed = [(path.rsplit("/", 1)[-1], peer) for path, peer in proxy.requests]
        assert observed == [
            ("a1", SOURCE_A), ("b1", SOURCE_B), ("a2", SOURCE_A), ("b2", SOURCE_B),
        ], "sessions using one proxy URL must retain separate source addresses"
        assert session_a.get_adapter(target.url).proxy_manager[proxy.url] is not \
            session_b.get_adapter(target.url).proxy_manager[proxy.url]
    assert target.peers == []


def test_n8_any_interface_is_an_ordinary_session(loopback_servers, monkeypatch):
    target, proxy = loopback_servers

    def unexpected_lookup(name):
        pytest.fail("Any interface must not resolve an interface address")

    monkeypatch.setattr(netiface, "interface_address", unexpected_lookup)
    with netiface.bound_requests_session("") as session:
        assert isinstance(session, requests.Session)
        assert session.trust_env is True
        _get(session, target.url + "/ordinary", "target-ok")
        adapter = session.get_adapter(target.url)
        assert adapter.poolmanager.connection_pool_kw.get("source_address") is None
        manager = adapter.proxy_manager_for(proxy.url)
        assert manager.connection_pool_kw.get("source_address") is None
    assert target.peers == ["127.0.0.1"]
    assert proxy.peers == []


def test_n9_unavailable_interface_fails_before_any_connection(
    loopback_servers, monkeypatch,
):
    target, proxy = loopback_servers
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    monkeypatch.setattr(netiface, "interface_address", lambda name: None)
    with pytest.raises(netiface.NetworkInterfaceUnavailable, match="gone-vpn"):
        with netiface.bound_requests_session("gone-vpn") as session:
            _get(session, target.url + "/must-not-send", "via-proxy")
    assert proxy.peers == []
    assert target.peers == []


def test_n10_trust_env_honors_http_proxy_and_no_proxy(
    loopback_servers, selected_interface, monkeypatch,
):
    target, proxy = loopback_servers
    monkeypatch.setenv("HTTP_PROXY", proxy.url)
    with netiface.bound_requests_session("synthetic-vpn") as session:
        assert session.trust_env is True
        _get(session, target.url + "/through-proxy", "via-proxy")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        _get(session, target.url + "/direct-after-no-proxy", "target-ok")
    assert len(proxy.requests) == 1
    assert target.peers == [SOURCE_A]


@pytest.mark.parametrize("bundle", ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
def test_n11_environment_ca_bundle_verifies_local_tls(
    loopback_alias, selected_interface, tls_certificate, monkeypatch, bundle,
):
    certificate, context = tls_certificate
    with _serve(_TargetHandler, tls_context=context) as target:
        url = f"https://127.0.0.1:{target.server_port}/trusted"
        with netiface.bound_requests_session("synthetic-vpn") as session:
            # The generated self-signed certificate must not be trusted by default.
            with pytest.raises(requests.exceptions.SSLError):
                session.get(url, timeout=5)
            monkeypatch.setenv(bundle, str(certificate))
            _get(session, url, "target-ok")
            merged = session.merge_environment_settings(url, {}, None, None, None)
            assert merged["verify"] == str(certificate)
            # Explicit verification choices keep their normal Requests semantics.
            assert session.merge_environment_settings(url, {}, None, False, None)["verify"] is False
            explicit = session.merge_environment_settings(url, {}, None, "explicit-ca.pem", None)
            assert explicit["verify"] == "explicit-ca.pem"
        assert target.requests == [("/trusted", SOURCE_A)]


def test_n11_requests_ca_bundle_precedes_curl_ca_bundle(selected_interface, monkeypatch):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "requests-ca.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "curl-ca.pem")
    with netiface.bound_requests_session("synthetic-vpn") as session:
        settings = session.merge_environment_settings("https://127.0.0.1/", {}, None, None, None)
        assert settings["verify"] == "requests-ca.pem"


def test_n12_socks_manager_receives_source_address(selected_interface, monkeypatch, record_property):
    runtime = "AVAILABLE" if importlib.util.find_spec("socks") is not None else "NOT AVAILABLE"
    print(f"SOCKS RUNTIME: {runtime}")
    record_property("socks_runtime", runtime)
    observed = []

    def recording_socks_manager(proxy, **kwargs):
        observed.append((proxy, kwargs))
        return SimpleNamespace(clear=lambda: None, connection_pool_kw=kwargs)

    monkeypatch.setattr(requests.adapters, "SOCKSProxyManager", recording_socks_manager)
    with netiface.bound_requests_session("synthetic-vpn") as session:
        adapter = session.get_adapter("http://127.0.0.1/")
        proxy = "socks5://test-user:test-pass@127.0.0.1:1080"
        manager = adapter.proxy_manager_for(proxy)
        assert adapter.proxy_manager_for(proxy) is manager
        assert len(observed) == 1
        assert observed[0][1]["username"] == "test-user"
        assert observed[0][1]["password"] == "test-pass"
        assert observed[0][1].get("source_address") == (SOURCE_A, 0), (
            "SOCKS manager did not receive the selected source address"
        )


def test_r1_requests_requirement_has_security_floor():
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    declarations = [
        line.split("#", 1)[0].strip()
        for line in requirements.read_text().splitlines()
        if re.match(r"^\s*requests\b", line, re.IGNORECASE)
    ]
    assert len(declarations) == 1, "expected one Requests requirement"
    assert not any(operator in declarations[0] for operator in ("<", "<=", "==", "~=", "!=")), (
        "Requests must have no upper bound or pin operator"
    )
    floors = re.findall(r">=\s*(\d+(?:\.\d+)*)", declarations[0])
    assert floors, "Requests must declare a minimum version"
    # Integer components compare versions correctly (2.32.10 > 2.32.4).
    versions = [tuple(int(part) for part in floor.split(".")) for floor in floors]
    assert max(version + (0,) * max(0, 3 - len(version)) for version in versions) >= (2, 32, 4), (
        f"{declarations[0]} permits Requests older than the 2.32.4 security floor"
    )
