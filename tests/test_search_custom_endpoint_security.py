"""Custom Torznab endpoint network-security policy and routing (slice S4).

These tests prove the pure classification and transport-decision policy, and
that ``TorznabSource`` applies it before any network request. Nothing here
touches a real network, DNS, interface or gateway: every request goes through a
fake session that records what it was asked and replays deterministic bytes.
"""
from pathlib import Path

import pytest
import requests

from cove.search.custom_endpoint import (
    EndpointClass,
    EndpointPolicyError,
    classify_custom_torznab_endpoint,
    classify_host,
    resolve_custom_torznab_transport,
)
from cove.search.indexers import CustomTorznabIndexer
from cove.search.models import Category, SourceError
from cove.search.sources.base import SearchHttp
from cove.search.sources.torznab import TorznabSource

# Two obvious fake sentinels so a leak is never missed by a test that forgot to
# insert them first.
API_KEY = "super-secret-s4-key"
QUERY_SECRET = "super-secret-query-value"

INDEXER_ID = "custom:22222222-2222-4222-8222-222222222222"

LOCAL = EndpointClass.LOCAL_LOOPBACK
PRIVATE = EndpointClass.PRIVATE_LAN
PUBLIC = EndpointClass.PUBLIC_OR_UNRESOLVED


# --- deterministic fake transport (minimal mirror of the S3 test harness) ----


class FakeResponse:
    def __init__(self, body: bytes = b"", status_code: int = 200, headers=None):
        self.body = body
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.closed = False
        # Mirrors requests.Session's default, so a test can prove the transport
        # never flips it off (which would also drop an environment CA bundle).
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0) if self.outcomes else FakeResponse(b"{}")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


def http_with(*outcomes):
    session = FakeSession(*outcomes)
    return SearchHttp(session=session), session


def make_indexer(**kwargs):
    fields = dict(
        id=INDEXER_ID,
        enabled=True,
        name="S4 Indexer",
        url="http://127.0.0.1:9696/api",
        api_key="",
    )
    fields.update(kwargs)
    return CustomTorznabIndexer(**fields)


def make_source(**kwargs):
    return TorznabSource(make_indexer(**kwargs))


def url_of(session, i):
    return session.calls[i][0]


def params_of(session, i):
    return session.calls[i][1]["params"]


def caps_doc() -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<caps><limits max="100" default="50"/>'
        b'<searching><search available="yes" supportedParams="q"/></searching>'
        b"</caps>"
    )


def caps_response() -> FakeResponse:
    return FakeResponse(caps_doc())


def usable_item(i: int) -> bytes:
    infohash = f"{i:040x}"
    return (
        f'<item><title>Item {i}</title>'
        f'<torznab:attr name="infohash" value="{infohash}"/>'
        f'<torznab:attr name="seeders" value="3"/>'
        f'<torznab:attr name="leechers" value="1"/>'
        f'<torznab:attr name="size" value="1000"/></item>'
    ).encode()


def feed(*items: bytes) -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">'
        b"<channel>"
        + b"".join(items)
        + b"</channel></rss>"
    )


# --- RED GROUP 1: basic classification --------------------------------------


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST", "localhost.", "127.0.0.1", "127.1.2.3", "::1"],
)
def test_loopback_hosts_classify_local(host):
    assert classify_host(host) is LOCAL


@pytest.mark.parametrize(
    "host",
    [
        "10.0.0.1",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.1.1",
        "fc00::1",
        "fd12:3456::1",
        "fe80::1",
    ],
)
def test_private_hosts_classify_lan(host):
    assert classify_host(host) is PRIVATE


@pytest.mark.parametrize("host", ["8.8.8.8", "2001:4860:4860::8888", "example.com"])
def test_public_hosts_classify_public(host):
    assert classify_host(host) is PUBLIC


# --- RED GROUP 2: hostname confusion ----------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "localhost.attacker.example",
        "my-localhost.example",
        "localhost-example.com",
        "example.localhost.com",
    ],
)
def test_localhost_lookalikes_are_not_privileged(host):
    assert classify_host(host) is PUBLIC


@pytest.mark.parametrize("host", ["localhost..", "localhost...", "localhost.evil"])
def test_localhost_with_extra_trailing_dots_is_not_privileged(host):
    # Only "localhost" and "localhost." are exact matches; extra trailing dots
    # are malformed and must not inherit local routing privilege.
    assert classify_host(host) is PUBLIC


# --- RED GROUP 3: 172.16/12 exact boundaries --------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("172.15.255.255", PUBLIC),
        ("172.16.0.0", PRIVATE),
        ("172.31.255.255", PRIVATE),
        ("172.32.0.0", PUBLIC),
    ],
)
def test_172_16_12_boundaries(host, expected):
    assert classify_host(host) is expected


# --- RED GROUP 4: other special IPv4 stays public ---------------------------


@pytest.mark.parametrize("host", ["0.0.0.0", "100.64.0.1", "224.0.0.1"])
def test_special_addresses_are_not_privileged(host):
    assert classify_host(host) is PUBLIC


# --- RED GROUP 5: IPv4-mapped IPv6 ------------------------------------------


@pytest.mark.parametrize(
    "host,expected",
    [
        ("::ffff:127.0.0.1", LOCAL),
        ("::ffff:192.168.1.10", PRIVATE),
        ("::ffff:8.8.8.8", PUBLIC),
    ],
)
def test_ipv4_mapped_ipv6_uses_ipv4_policy(host, expected):
    assert classify_host(host) is expected


# --- RED GROUP 6: no DNS classification -------------------------------------


def test_classification_never_resolves_hostnames(monkeypatch):
    import socket

    def boom(*args, **kwargs):
        raise AssertionError("DNS resolution must not be used for classification")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    monkeypatch.setattr(socket, "gethostbyname_ex", boom)

    for host in ("prowlarr.lan", "nas.home", "myserver.local", "example.com"):
        assert classify_host(host) is PUBLIC


# --- RED GROUP 7: malformed authority ---------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "example.com/api",  # missing scheme -> no hostname
        "http:///path",  # missing hostname
        "http://[::1/",  # broken bracketed IPv6
        "http://[invalid]/",  # invalid bracketed literal
        "http://host:99999/api",  # out-of-range port
        "http://host:abc/api",  # non-numeric port
    ],
)
def test_malformed_authority_rejected_before_network(url):
    policy = resolve_custom_torznab_transport(url, "wg0")
    assert policy.allowed is False
    assert policy.reason is not None
    assert url not in policy.reason


def test_url_level_classifier_rejects_malformed_authority():
    with pytest.raises(EndpointPolicyError):
        classify_custom_torznab_endpoint("http://[::1/")
    with pytest.raises(EndpointPolicyError):
        classify_custom_torznab_endpoint("http://host:99999/api")


# --- RED GROUP 8: URL userinfo ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["https://user@example.com/api", "https://user:password@example.com/api"],
)
def test_url_userinfo_rejected_before_network(url):
    policy = resolve_custom_torznab_transport(url, "wg0")
    assert policy.allowed is False
    assert policy.reason == "Torznab endpoint URL credentials are not supported"
    assert "user" not in policy.reason
    assert "password" not in policy.reason


# --- RED GROUP 9/10: local/private HTTP + interface bypass ------------------


@pytest.mark.parametrize(
    "url,expected_class",
    [
        ("http://localhost:9696/api", LOCAL),
        ("http://127.0.0.1:9696/api", LOCAL),
        ("http://192.168.1.20:9696/api", PRIVATE),
        ("http://10.0.0.20:9117/api", PRIVATE),
        ("http://[::1]:9696/api", LOCAL),
        ("http://[fd12:3456::1]/api", PRIVATE),
        ("http://[fe80::1]/api", PRIVATE),
    ],
)
def test_local_private_http_allowed_and_bypasses_interface(url, expected_class):
    policy = resolve_custom_torznab_transport(url, "wg0")
    assert policy.allowed is True
    assert policy.classification is expected_class
    assert policy.effective_interface is None
    assert policy.suppress_env_proxy is True


def test_local_endpoint_bypasses_interface_in_transport():
    source = make_source(url="http://127.0.0.1:9696/api")
    session = FakeSession(caps_response(), FakeResponse(feed(usable_item(1))))
    http = SearchHttp(session=session, interface="wg0")

    source.search("hello", Category.ALL, http)

    assert http.interface == "wg0"  # requested interface preserved
    assert http.effective_interface == ""  # binding bypassed
    assert session.trust_env is True  # CA bundle never disabled
    assert session.calls[0][1]["proxies"] == {"http": None, "https": None, "all": None}
    assert len(session.calls) == 2  # caps + one short page


# --- RED GROUP 11: public HTTPS preserves caller interface ------------------


@pytest.mark.parametrize(
    "url,requested,expected",
    [
        ("https://example.com/api", "wg0", "wg0"),
        ("https://8.8.8.8/api", "wg0", "wg0"),
        ("https://example.com/api", None, None),
        ("https://example.com/api", "", ""),
    ],
)
def test_public_https_preserves_caller_interface(url, requested, expected):
    policy = resolve_custom_torznab_transport(url, requested)
    assert policy.allowed is True
    assert policy.classification is PUBLIC
    assert policy.effective_interface == expected
    assert policy.suppress_env_proxy is False


def test_public_https_keeps_interface_in_transport():
    source = make_source(url="https://example.com/api")
    session = FakeSession(caps_response(), FakeResponse(feed(usable_item(1))))
    http = SearchHttp(session=session, interface="wg0")

    source.search("hello", Category.ALL, http)

    assert http.interface == "wg0"  # requested interface preserved
    assert http.effective_interface == "wg0"  # caller interface preserved
    assert session.trust_env is True  # env-proxy/CA default untouched
    assert "proxies" not in session.calls[0][1]  # no proxy override
    assert len(session.calls) == 2


# --- RED GROUP 12: public HTTP rejected pre-network -------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/api",
        "http://8.8.8.8/api",
        "http://172.15.1.1/api",
        "http://172.32.1.1/api",
        "http://prowlarr.lan/api",
        "http://0.0.0.0/api",
    ],
)
def test_public_http_rejected(url):
    policy = resolve_custom_torznab_transport(url, "wg0")
    assert policy.allowed is False
    assert policy.reason == "Torznab public endpoints require HTTPS"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/api",
        "http://8.8.8.8/api",
        "http://prowlarr.lan/api",
    ],
)
def test_public_http_rejection_makes_zero_requests(url):
    source = make_source(url=url)
    http, session = http_with()
    with pytest.raises(SourceError):
        source.search("hello", Category.ALL, http)
    assert session.calls == []


# --- RED GROUP 13: unsupported schemes --------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/api",
        "ws://example.com/api",
        "wss://example.com/api",
        "unknown://example.com/api",
    ],
)
def test_unsupported_scheme_rejected(url):
    policy = resolve_custom_torznab_transport(url, "wg0")
    assert policy.allowed is False
    assert policy.reason == "Unsupported Torznab endpoint scheme"


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "data:text/plain,hi", "javascript:alert(1)"],
)
def test_non_authority_schemes_rejected(url):
    # These have no parseable authority, so they reject as an invalid endpoint
    # rather than an unsupported scheme. Either way: rejected, before network.
    policy = resolve_custom_torznab_transport(url, "wg0")
    assert policy.allowed is False


@pytest.mark.parametrize(
    "url",
    ["ftp://example.com/api", "file:///etc/passwd", "ws://example.com/api"],
)
def test_unsupported_scheme_makes_zero_requests(url):
    source = make_source(url=url)
    http, session = http_with()
    with pytest.raises(SourceError):
        source.search("hello", Category.ALL, http)
    assert session.calls == []


# --- RED GROUP 14: policy failures never leak secrets -----------------------


@pytest.mark.parametrize(
    "url",
    [
        f"http://example.com/api?token={QUERY_SECRET}",  # public HTTP
        f"ftp://example.com/api?token={QUERY_SECRET}",  # unsupported scheme
        f"http://example.com:99999/api?token={QUERY_SECRET}",  # malformed port
        f"https://user:{QUERY_SECRET}@example.com/api",  # userinfo
    ],
)
def test_policy_failure_leaks_no_secret(url):
    source = make_source(url=url, api_key=API_KEY)
    http, session = http_with()
    with pytest.raises(SourceError) as excinfo:
        source.search("hello", Category.ALL, http)

    for surface in (str(excinfo.value), repr(excinfo.value)):
        assert API_KEY not in surface
        assert QUERY_SECRET not in surface
    assert session.calls == []


# --- RED GROUP 15: environment-proxy discipline -----------------------------


def test_default_search_http_keeps_environment_proxy_inheritance():
    # No apply_routing: the session keeps requests' default trust_env=True, so
    # an environment proxy and an environment CA bundle both still apply.
    http = SearchHttp(interface="")
    assert http.session().trust_env is True


def test_local_routing_suppresses_env_proxy_without_disabling_ca():
    # The local path must block environment proxies per request, but must NOT
    # set trust_env=False (that would also drop REQUESTS_CA_BUNDLE /
    # CURL_CA_BUNDLE and break private-HTTPS verification).
    session = FakeSession(caps_response(), FakeResponse(feed(usable_item(1))))
    http = SearchHttp(session=session, interface="wg0")
    source = make_source(url="http://127.0.0.1:9696/api")

    source.search("hello", Category.ALL, http)

    assert session.trust_env is True  # CA bundle preserved (never disabled)
    for _, kwargs in session.calls:
        assert kwargs["proxies"] == {"http": None, "https": None, "all": None}


def test_public_routing_keeps_environment_proxy_inheritance():
    session = FakeSession(caps_response(), FakeResponse(feed(usable_item(1))))
    http = SearchHttp(session=session, interface="wg0")
    source = make_source(url="https://example.com/api")

    source.search("hello", Category.ALL, http)

    assert session.trust_env is True
    for _, kwargs in session.calls:
        assert "proxies" not in kwargs  # env proxy merge left to requests default


def test_all_proxy_is_suppressed_for_local_endpoints(monkeypatch):
    # Requests consults an ``all`` fallback proxy when no scheme-specific entry
    # remains, so a bare ALL_PROXY must be neutralised too, and the CA bundle
    # must survive the suppression.
    import requests as requests_mod

    from cove.search.sources.base import _NO_ENV_PROXY

    monkeypatch.setenv("ALL_PROXY", "http://allproxy.example:9999")
    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/tmp/fake-ca.pem")

    session = requests_mod.Session()
    merged = session.merge_environment_settings(
        "http://127.0.0.1/x", dict(_NO_ENV_PROXY), False, True, None
    )

    assert merged["proxies"] == {}  # ALL_PROXY and http_proxy both suppressed
    assert merged["verify"] == "/tmp/fake-ca.pem"  # CA bundle preserved


# --- RED GROUP 16/17: redirects are never followed --------------------------


@pytest.mark.parametrize(
    "url,location",
    [
        ("http://127.0.0.1:9696/api", "http://example.com/other"),
        ("http://127.0.0.1:9696/api", "http://127.0.0.1/other"),
        ("https://example.com/api", "http://example.com/api2"),
    ],
)
def test_redirect_is_not_followed(url, location):
    source = make_source(url=url)
    redirect = FakeResponse(b"", status_code=302, headers={"Location": location})
    http, session = http_with(redirect)
    with pytest.raises(SourceError):
        source.search("hello", Category.ALL, http)
    assert len(session.calls) == 1  # no follow-up request
    assert session.calls[0][1]["allow_redirects"] is False


# --- RED GROUP 18: no TLS bypass surface ------------------------------------


def test_s4_policy_module_has_no_tls_knob():
    source_path = Path(__file__).parent.parent / "cove" / "search" / "custom_endpoint.py"
    text = source_path.read_text()
    assert "verify" not in text
    assert "CERT_NONE" not in text
    assert "check_hostname" not in text
    assert "import ssl" not in text
    assert "import requests" not in text


# --- RED GROUP 19: built-in containment -------------------------------------


def test_builtin_adapters_do_not_import_custom_policy():
    sources = Path(__file__).parent.parent / "cove" / "search" / "sources"
    builtin = [
        "fitgirl.py",
        "goggames.py",
        "nekobt.py",
        "nyaa.py",
        "piratebay.py",
        "subsplease.py",
        "torrentscsv.py",
        "yts.py",
    ]
    for name in builtin:
        text = (sources / name).read_text()
        assert "custom_endpoint" not in text, name


def test_search_http_defaults_unchanged_without_apply_routing():
    # No apply_routing call: the interface report, the effective interface and
    # the environment-proxy default are exactly the pre-S4 behaviour.
    assert SearchHttp(interface="wg0").interface == "wg0"
    assert SearchHttp(interface="").interface == ""
    assert SearchHttp(interface="wg0").effective_interface == "wg0"
    assert SearchHttp(interface="").session().trust_env is True


# --- RED GROUP 19b: routing actually reaches the session (Codex round 1) -----


def test_injected_session_receives_routing_policy():
    session = FakeSession(caps_response(), FakeResponse(feed(usable_item(1))))
    http = SearchHttp(session=session, interface="wg0")

    http.apply_routing("", suppress_env_proxy=True)
    assert http.effective_interface == ""
    assert http.interface == "wg0"  # requested interface preserved

    # Reusing the same transport for a public endpoint restores the policy.
    http.apply_routing("wg0", suppress_env_proxy=False)
    assert http.effective_interface == "wg0"
    assert http.interface == "wg0"


def test_owned_session_rebuilds_when_interface_changes(monkeypatch):
    import cove.netiface as netiface

    built = []

    def fake_bound(name):
        session = FakeSession()
        built.append((name, session))
        return session

    monkeypatch.setattr(netiface, "bound_requests_session", fake_bound)

    http = SearchHttp(interface="")
    first = http.session()  # materialise the unbound session
    assert built == [("", first)]

    http.apply_routing("wg0", suppress_env_proxy=False)  # interface changed
    assert http.session() is not first
    assert built[1][0] == "wg0"
    assert http.effective_interface == "wg0"


def test_owned_session_survives_proxy_only_change(monkeypatch):
    import cove.netiface as netiface

    monkeypatch.setattr(netiface, "bound_requests_session", lambda name: FakeSession())

    http = SearchHttp(interface="")
    first = http.session()

    http.apply_routing("", suppress_env_proxy=True)  # same interface, proxy only
    assert http.session() is first  # no rebuild

    http.apply_routing("", suppress_env_proxy=False)
    assert http.session() is first


def test_policy_uses_snapshotted_endpoint_not_live_field():
    # The security decision and the actual request must act on the same
    # immutable endpoint; mutating the record's url after construction must
    # not let policy and transport target different destinations.
    source = make_source(url="https://example.com/api")
    source.indexer.url = "http://127.0.0.1:9696/api"  # mutate live field
    session = FakeSession(caps_response(), FakeResponse(feed(usable_item(1))))
    http = SearchHttp(session=session, interface="wg0")

    source.search("hello", Category.ALL, http)

    assert url_of(session, 0) == "https://example.com/api"  # snapshot, not live
    assert http.effective_interface == "wg0"  # public HTTPS -> interface preserved


def test_endpoint_url_is_read_exactly_once_during_construction():
    # A single immutable snapshot: policy and request both derive from one read.
    # A second read of the mutable field could let the two diverge.
    reads = []

    class Spy:
        id = INDEXER_ID
        name = "Spy"
        enabled = True
        api_key = ""

        def __init__(self, url):
            self._url = url

        @property
        def url(self):
            reads.append(self._url)
            return self._url

    TorznabSource(Spy("https://example.com/api"))

    assert reads == ["https://example.com/api"]


# --- RED GROUP 20: S3 machinery preserved -----------------------------------
def test_s4_plumbing_does_not_bypass_s3_flow():
    url = "http://127.0.0.1:9696/api/v2.0/indexers/x/results/torznab/api?foo=bar"
    source = make_source(url=url, api_key="key123")
    session = FakeSession(caps_response(), FakeResponse(feed(usable_item(1))))
    http = SearchHttp(session=session, interface="wg0")

    results = source.search("hello world", Category.ALL, http)

    # Caps first, then one search page (a one-item page is short, so it stops).
    assert len(session.calls) == 2
    assert url_of(session, 0) == "http://127.0.0.1:9696/api/v2.0/indexers/x/results/torznab/api"
    caps_params = dict(params_of(session, 0))
    assert caps_params["t"] == "caps"
    page_params = params_of(session, 1)
    page = dict(page_params)
    assert page["t"] == "search"
    assert page["q"] == "hello world"  # one semantic multi-word query value
    assert page["apikey"] == "key123"
    # Unrelated configured query parameter preserved exactly once.
    assert ("foo", "bar") in page_params
    assert len([v for k, v in page_params if k == "foo"]) == 1
    assert results != []
    assert http.interface == "wg0"  # requested interface preserved
    assert http.effective_interface == ""  # local endpoint bypassed the binding


# --- RED GROUP 21: rejected endpoints validate before caps ------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/api",
        "ftp://example.com/api",
        "https://user:pass@example.com/api",
        "http://example.com:99999/api",
    ],
)
def test_rejection_happens_before_any_caps_or_search_request(url):
    source = make_source(url=url)
    http, session = http_with(caps_response(), FakeResponse(feed(usable_item(1))))
    with pytest.raises(SourceError):
        source.search("hello", Category.ALL, http)
    assert session.calls == []  # zero caps, zero search pages
