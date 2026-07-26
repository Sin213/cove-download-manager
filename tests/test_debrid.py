"""Tests for the Real-Debrid / AllDebrid link resolution layer.

Every provider interaction is mocked; nothing here touches the network.
The security-flavoured tests exist because a leaked API key or a leaked
generated node URL is the worst failure mode of this feature, and both
are easy to reintroduce accidentally through an exception message.
"""

import json

import pytest

from cove import config, debrid
from cove.config import Settings
from cove.debrid import ALL_DEBRID, REAL_DEBRID, DebridError, Unrestricted

# Fabricated credentials - not real, and not valid at either provider. The
# "SECRET" substring is the canary the leak tests below grep for, so it has
# to stay distinctive. gitleaks:allow marks these as known test fixtures.
TOKEN = "rd-token-SECRET-0123456789"  # gitleaks:allow
APIKEY = "ad-apikey-SECRET-0123456789"  # gitleaks:allow


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Routes requests by URL suffix. A list value is consumed in order."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def get(self, url, **kwargs):
        return self._handle("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._handle("POST", url, kwargs)

    def head(self, url, **kwargs):  # pragma: no cover - unused here
        return self._handle("HEAD", url, kwargs)

    def put(self, url, **kwargs):
        return self._handle("PUT", url, kwargs)

    def delete(self, url, **kwargs):
        return self._handle("DELETE", url, kwargs)

    def _handle(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for suffix, value in self.routes.items():
            if url.endswith(suffix):
                if isinstance(value, list):
                    if not value:
                        raise AssertionError(f"no queued response left for {suffix}")
                    return value.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unrouted request: {method} {url}")


class FakeClock:
    """Monotonic clock that only advances when the fake sleep is called."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Keep the host-domain cache out of the real Cove data directory."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path


def _settings(**kwargs):
    base = dict(
        all_debrid_enabled=False,
        all_debrid_api_key="",
        real_debrid_enabled=False,
        real_debrid_api_token="",
        debrid_preferred_provider="alldebrid",
    )
    base.update(kwargs)
    return Settings(**base)


def _seed_cache(tmp_path, provider, domains, fetched_at=None):
    import time as _time

    path = tmp_path / "debrid_hosts.json"
    data = {}
    if path.exists():
        data = json.loads(path.read_text())
    data[provider] = {
        "fetched_at": _time.time() if fetched_at is None else fetched_at,
        "domains": domains,
    }
    path.write_text(json.dumps(data))
    return path


# --------------------------------------------------------------------------
# Real-Debrid
# --------------------------------------------------------------------------


def test_real_debrid_unrestrict_returns_download_filename_and_size():
    session = FakeSession({
        "/unrestrict/link": _Resp({
            "download": "https://node-01.real-debrid.com/d/ABCDEF/file.zip",
            "filename": "file.zip",
            "filesize": 12345678,
            "chunks": 16,
        }),
    })
    result = debrid.real_debrid_unrestrict(
        "https://rapidgator.net/file/abc", TOKEN, session=session
    )
    assert isinstance(result, Unrestricted)
    assert result.download == "https://node-01.real-debrid.com/d/ABCDEF/file.zip"
    assert result.filename == "file.zip"
    assert result.filesize == 12345678
    assert result.provider == REAL_DEBRID


def test_real_debrid_unrestrict_sends_bearer_token_and_form_link():
    session = FakeSession({
        "/unrestrict/link": _Resp({
            "download": "https://node-01.real-debrid.com/d/A/f.zip",
            "filename": "f.zip",
            "filesize": 10,
        }),
    })
    debrid.real_debrid_unrestrict("https://rapidgator.net/file/abc", TOKEN, session=session)
    _method, _url, kwargs = session.calls[0]
    assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert kwargs["data"] == {"link": "https://rapidgator.net/file/abc"}


def test_real_debrid_zero_filesize_is_accepted():
    session = FakeSession({
        "/unrestrict/link": _Resp({
            "download": "https://node-01.real-debrid.com/d/A/f.zip",
            "filename": "f.zip",
            "filesize": 0,
        }),
    })
    result = debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)
    assert result.filesize == 0


def test_real_debrid_rejects_non_http_generated_url():
    session = FakeSession({
        "/unrestrict/link": _Resp({
            "download": "file:///etc/passwd",
            "filename": "f.zip",
            "filesize": 10,
        }),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)
    assert excinfo.value.fallback_allowed is False


def test_real_debrid_rejects_missing_download_field():
    session = FakeSession({
        "/unrestrict/link": _Resp({"filename": "f.zip", "filesize": 10}),
    })
    with pytest.raises(DebridError):
        debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)


def test_real_debrid_rejects_unparseable_json():
    session = FakeSession({
        "/unrestrict/link": _Resp(ValueError("not json"), status_code=200),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)
    assert excinfo.value.fallback_allowed is False


@pytest.mark.parametrize(
    "code,fallback",
    [
        (8, False),    # bad token
        (9, False),    # permission denied
        (14, False),   # account locked
        (16, True),    # unsupported hoster
        (17, True),    # hoster in maintenance
        (18, True),    # hoster limit reached
        (19, True),    # hoster temporarily unavailable
        (20, False),   # unavailable for free users
        (21, True),    # too many active downloads
        (23, True),    # traffic exhausted
        (24, False),   # file unavailable
        (25, True),    # service unavailable
        (34, True),    # too many requests
        (35, False),   # infringing file
        (36, True),    # fair usage limit
    ],
)
def test_real_debrid_error_codes_map_to_expected_fallback_policy(code, fallback):
    session = FakeSession({
        "/unrestrict/link": _Resp(
            {"error": "some_error", "error_code": code}, status_code=403
        ),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)
    err = excinfo.value
    assert err.provider == REAL_DEBRID
    assert err.code == code
    assert err.fallback_allowed is fallback
    assert err.user_message


def test_real_debrid_unsupported_hoster_is_flagged_host_unsupported():
    session = FakeSession({
        "/unrestrict/link": _Resp({"error": "unsupported_hoster", "error_code": 16}, 403),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_unrestrict("https://nope.example/f", TOKEN, session=session)
    assert excinfo.value.host_unsupported is True


def test_real_debrid_unknown_error_code_does_not_fall_back():
    session = FakeSession({
        "/unrestrict/link": _Resp({"error": "weird", "error_code": 9999}, 403),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)
    assert excinfo.value.fallback_allowed is False


def test_real_debrid_http_error_without_error_envelope_is_reported():
    session = FakeSession({"/unrestrict/link": _Resp({}, status_code=500)})
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)
    assert "Real-Debrid" in str(excinfo.value)


def test_real_debrid_account_returns_sanitized_summary():
    session = FakeSession({
        "/user": _Resp({
            "id": 1,
            "username": "coveuser",
            "email": "user@example.com",
            "type": "premium",
            "expiration": "2027-01-01T00:00:00.000Z",
        }),
    })
    account = debrid.real_debrid_account(TOKEN, session=session)
    assert account["username"] == "coveuser"
    assert account["type"] == "premium"
    assert account["expiration"] == "2027-01-01T00:00:00.000Z"
    assert "email" not in account


def test_real_debrid_account_bad_token_raises_non_fallback():
    session = FakeSession({
        "/user": _Resp({"error": "bad_token", "error_code": 8}, status_code=401),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_account(TOKEN, session=session)
    assert excinfo.value.fallback_allowed is False


def test_real_debrid_supported_domains_parses_flat_array():
    session = FakeSession({
        "/hosts/domains": _Resp(["rapidgator.net", "1FICHIER.COM", 42, ""]),
    })
    domains = debrid.real_debrid_supported_domains(session=session)
    assert "rapidgator.net" in domains
    assert "1fichier.com" in domains
    assert 42 not in domains
    assert "" not in domains


# --------------------------------------------------------------------------
# AllDebrid
# --------------------------------------------------------------------------


def test_all_debrid_direct_unlock_returns_link_filename_and_size():
    session = FakeSession({
        "/link/unlock": _Resp({
            "status": "success",
            "data": {
                "link": "https://s1.debrid.it/dl/abc/file.zip",
                "filename": "file.zip",
                "filesize": 987654,
                "host": "rapidgator",
            },
        }),
    })
    result = debrid.all_debrid_unrestrict(
        "https://rapidgator.net/file/abc", APIKEY, session=session
    )
    assert result.download == "https://s1.debrid.it/dl/abc/file.zip"
    assert result.filename == "file.zip"
    assert result.filesize == 987654
    assert result.provider == ALL_DEBRID


def test_all_debrid_unlock_sends_bearer_apikey_and_form_link():
    session = FakeSession({
        "/link/unlock": _Resp({
            "status": "success",
            "data": {"link": "https://s1.debrid.it/dl/a/f.zip", "filename": "f.zip",
                     "filesize": 1},
        }),
    })
    debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)
    _method, _url, kwargs = session.calls[0]
    assert kwargs["headers"]["Authorization"] == f"Bearer {APIKEY}"
    assert kwargs["data"] == {"link": "https://rapidgator.net/f"}


def test_all_debrid_zero_filesize_is_accepted():
    session = FakeSession({
        "/link/unlock": _Resp({
            "status": "success",
            "data": {"link": "https://s1.debrid.it/dl/a/f.zip", "filename": "f.zip",
                     "filesize": 0},
        }),
    })
    result = debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)
    assert result.filesize == 0


def test_all_debrid_rejects_non_http_generated_link():
    session = FakeSession({
        "/link/unlock": _Resp({
            "status": "success",
            "data": {"link": "ftp://s1.debrid.it/f.zip", "filename": "f.zip", "filesize": 1},
        }),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)
    assert excinfo.value.fallback_allowed is False


def test_all_debrid_rejects_non_success_envelope():
    session = FakeSession({"/link/unlock": _Resp({"status": "weird", "data": {}})})
    with pytest.raises(DebridError):
        debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)


def test_all_debrid_rejects_missing_data_object():
    session = FakeSession({"/link/unlock": _Resp({"status": "success"})})
    with pytest.raises(DebridError):
        debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)


def test_all_debrid_delayed_link_completes_on_status_two():
    clock = FakeClock()
    session = FakeSession({
        "/link/unlock": _Resp({
            "status": "success",
            "data": {"delayed": 4242, "filename": "big.mkv", "filesize": 5000},
        }),
        "/link/delayed": [
            _Resp({"status": "success", "data": {"status": 1}}),
            _Resp({"status": "success", "data": {"status": 1}}),
            _Resp({"status": "success", "data": {
                "status": 2, "link": "https://s3.debrid.it/dl/z/big.mkv"}}),
        ],
    })
    result = debrid.all_debrid_unrestrict(
        "https://rapidgator.net/f", APIKEY,
        session=session, sleep=clock.sleep, clock=clock.monotonic,
    )
    assert result.download == "https://s3.debrid.it/dl/z/big.mkv"
    # Metadata from the unlock response survives the delayed round trip.
    assert result.filename == "big.mkv"
    assert result.filesize == 5000


def test_all_debrid_delayed_polls_no_faster_than_five_seconds():
    clock = FakeClock()
    session = FakeSession({
        "/link/unlock": _Resp({"status": "success", "data": {"delayed": 1, "filename": "a"}}),
        "/link/delayed": [
            _Resp({"status": "success", "data": {"status": 1}}),
            _Resp({"status": "success", "data": {"status": 1}}),
            _Resp({"status": "success", "data": {
                "status": 2, "link": "https://s3.debrid.it/dl/z/a"}}),
        ],
    })
    debrid.all_debrid_unrestrict(
        "https://rapidgator.net/f", APIKEY,
        session=session, sleep=clock.sleep, clock=clock.monotonic,
    )
    assert clock.slept, "delayed polling must wait between attempts"
    assert all(s >= 5.0 for s in clock.slept)


def test_all_debrid_delayed_status_three_is_a_generation_error():
    clock = FakeClock()
    session = FakeSession({
        "/link/unlock": _Resp({"status": "success", "data": {"delayed": 7}}),
        "/link/delayed": _Resp({"status": "success", "data": {"status": 3}}),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_unrestrict(
            "https://rapidgator.net/f", APIKEY,
            session=session, sleep=clock.sleep, clock=clock.monotonic,
        )
    assert excinfo.value.fallback_allowed is False


def test_all_debrid_delayed_times_out_without_looping_forever():
    clock = FakeClock()
    session = FakeSession({
        "/link/unlock": _Resp({"status": "success", "data": {"delayed": 7}}),
        "/link/delayed": _Resp({"status": "success", "data": {"status": 1}}),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_unrestrict(
            "https://rapidgator.net/f", APIKEY,
            session=session, sleep=clock.sleep, clock=clock.monotonic,
        )
    assert "AllDebrid" in str(excinfo.value)
    # Bounded: 60s deadline at >=5s per poll can never exceed 13 attempts.
    assert len(clock.slept) <= 13
    assert clock.now - 1000.0 <= debrid.DELAYED_MAX_WAIT + debrid.DELAYED_POLL_INTERVAL


def test_all_debrid_delayed_with_malformed_status_fails_cleanly():
    clock = FakeClock()
    session = FakeSession({
        "/link/unlock": _Resp({"status": "success", "data": {"delayed": 7}}),
        "/link/delayed": _Resp({"status": "success", "data": {"status": "soon"}}),
    })
    with pytest.raises(DebridError):
        debrid.all_debrid_unrestrict(
            "https://rapidgator.net/f", APIKEY,
            session=session, sleep=clock.sleep, clock=clock.monotonic,
        )


def test_all_debrid_delayed_status_two_without_link_fails_cleanly():
    clock = FakeClock()
    session = FakeSession({
        "/link/unlock": _Resp({"status": "success", "data": {"delayed": 7}}),
        "/link/delayed": _Resp({"status": "success", "data": {"status": 2}}),
    })
    with pytest.raises(DebridError):
        debrid.all_debrid_unrestrict(
            "https://rapidgator.net/f", APIKEY,
            session=session, sleep=clock.sleep, clock=clock.monotonic,
        )


def test_all_debrid_unlock_without_link_or_delayed_fails_cleanly():
    session = FakeSession({
        "/link/unlock": _Resp({"status": "success", "data": {"filename": "f.zip"}}),
    })
    with pytest.raises(DebridError):
        debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)


@pytest.mark.parametrize(
    "code,fallback",
    [
        ("AUTH_MISSING_APIKEY", False),
        ("AUTH_BAD_APIKEY", False),
        ("AUTH_BLOCKED", False),
        ("AUTH_USER_BANNED", False),
        ("MAINTENANCE", True),
        ("NO_SERVER", False),
        ("LINK_HOST_NOT_SUPPORTED", True),
        ("LINK_DOWN", False),
        ("LINK_HOST_UNAVAILABLE", True),
        ("LINK_TOO_MANY_DOWNLOADS", True),
        ("LINK_HOST_FULL", True),
        ("LINK_HOST_LIMIT_REACHED", True),
        ("LINK_PASS_PROTECTED", False),
        ("LINK_ERROR", False),
        ("LINK_NOT_SUPPORTED", True),
        ("LINK_TEMPORARY_UNAVAILABLE", True),
        ("MUST_BE_PREMIUM", False),
        ("FREE_TRIAL_LIMIT_REACHED", False),
        ("DELAYED_INVALID_ID", False),
    ],
)
def test_all_debrid_error_codes_map_to_expected_fallback_policy(code, fallback):
    session = FakeSession({
        "/link/unlock": _Resp({
            "status": "error",
            "error": {"code": code, "message": "provider text"},
        }),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)
    err = excinfo.value
    assert err.provider == ALL_DEBRID
    assert err.code == code
    assert err.fallback_allowed is fallback
    assert err.user_message


def test_all_debrid_unsupported_host_codes_are_flagged_host_unsupported():
    for code in ("LINK_HOST_NOT_SUPPORTED", "LINK_NOT_SUPPORTED"):
        session = FakeSession({
            "/link/unlock": _Resp({"status": "error", "error": {"code": code}}),
        })
        with pytest.raises(DebridError) as excinfo:
            debrid.all_debrid_unrestrict("https://x.example/f", APIKEY, session=session)
        assert excinfo.value.host_unsupported is True


def test_all_debrid_unknown_error_code_does_not_fall_back():
    session = FakeSession({
        "/link/unlock": _Resp({"status": "error", "error": {"code": "SOMETHING_NEW"}}),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)
    assert excinfo.value.fallback_allowed is False


def test_all_debrid_account_returns_sanitized_summary():
    session = FakeSession({
        "/v4/user": _Resp({
            "status": "success",
            "data": {"user": {
                "username": "coveuser",
                "email": "user@example.com",
                "isPremium": True,
                "isTrial": False,
                "premiumUntil": 1800000000,
            }},
        }),
    })
    account = debrid.all_debrid_account(APIKEY, session=session)
    assert account["username"] == "coveuser"
    assert account["is_premium"] is True
    assert account["is_trial"] is False
    assert account["premium_until"] == 1800000000
    assert "email" not in account


def test_all_debrid_account_bad_apikey_raises_non_fallback():
    session = FakeSession({
        "/v4/user": _Resp({"status": "error", "error": {"code": "AUTH_BAD_APIKEY"}}),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_account(APIKEY, session=session)
    assert excinfo.value.fallback_allowed is False


def test_all_debrid_supported_domains_uses_only_hosts():
    session = FakeSession({
        "/hosts/domains": _Resp({
            "status": "success",
            "data": {
                "hosts": ["rapidgator.net", "1FICHIER.COM"],
                "streams": ["youtube.com"],
                "redirectors": ["bit.ly"],
            },
        }),
    })
    domains = debrid.all_debrid_supported_domains(session=session)
    assert "rapidgator.net" in domains
    assert "1fichier.com" in domains
    assert "youtube.com" not in domains
    assert "bit.ly" not in domains


# --------------------------------------------------------------------------
# Host matching
# --------------------------------------------------------------------------


def test_exact_host_matches():
    assert debrid.is_supported_domain("https://rapidgator.net/file/1", ["rapidgator.net"])


def test_subdomain_matches_on_a_dot_boundary():
    assert debrid.is_supported_domain("https://foo.rapidgator.net/f", ["rapidgator.net"])
    assert debrid.is_supported_domain("https://a.b.rapidgator.net/f", ["rapidgator.net"])


def test_substring_lookalike_hosts_are_rejected():
    for host in (
        "https://notrapidgator.net/f",
        "https://rapidgator.net.evil.com/f",
        "https://evil.com/?x=rapidgator.net",
        "https://rapidgator.network/f",
    ):
        assert not debrid.is_supported_domain(host, ["rapidgator.net"]), host


def test_host_matching_is_case_insensitive():
    assert debrid.is_supported_domain("https://RapidGator.NET/f", ["rapidgator.net"])
    assert debrid.is_supported_domain("https://rapidgator.net/f", ["RapidGator.Net"])


def test_non_http_schemes_never_match():
    assert not debrid.is_supported_domain("ftp://rapidgator.net/f", ["rapidgator.net"])
    assert not debrid.is_supported_domain("magnet:?xt=urn:btih:abc", ["rapidgator.net"])


@pytest.mark.parametrize(
    "url",
    [
        "https://real-debrid.com/d/ABCDEF",
        "https://www.real-debrid.com/d/ABCDEF",
        "https://node-01.real-debrid.com/d/ABC/f.zip",
        "https://alldebrid.com/f/xyz",
        "https://www.alldebrid.com/f/xyz",
        "https://s1.debrid.it/dl/abc/file.zip",
        "https://debrid.it/dl/abc",
    ],
)
def test_provider_owned_urls_are_excluded(url):
    assert debrid.is_provider_domain(url) is True


def test_ordinary_hoster_url_is_not_a_provider_domain():
    assert debrid.is_provider_domain("https://rapidgator.net/file/1") is False
    # Lookalike must not be treated as provider-owned either.
    assert debrid.is_provider_domain("https://notalldebrid.com/f") is False


# --------------------------------------------------------------------------
# Host-domain cache
# --------------------------------------------------------------------------


def test_fresh_cache_is_used_without_any_request(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    session = FakeSession({})  # any request would raise
    assert debrid.supported_domains(ALL_DEBRID, session=session) == ["rapidgator.net"]
    assert session.calls == []


def test_expired_cache_triggers_a_refresh(tmp_path):
    import time as _time

    _seed_cache(tmp_path, ALL_DEBRID, ["old.example"],
                fetched_at=_time.time() - debrid.HOST_CACHE_TTL - 60)
    session = FakeSession({
        "/hosts/domains": _Resp({"status": "success", "data": {"hosts": ["new.example"]}}),
    })
    assert debrid.supported_domains(ALL_DEBRID, session=session) == ["new.example"]
    assert session.calls, "an expired cache must trigger a fetch"
    # Refresh is written back for the next call.
    assert debrid.supported_domains(ALL_DEBRID, session=FakeSession({})) == ["new.example"]


def test_stale_cache_is_used_when_the_refresh_fails(tmp_path):
    import time as _time

    _seed_cache(tmp_path, REAL_DEBRID, ["stale.example"],
                fetched_at=_time.time() - debrid.HOST_CACHE_TTL - 60)
    session = FakeSession({"/hosts/domains": OSError("network down")})
    assert debrid.supported_domains(REAL_DEBRID, session=session) == ["stale.example"]


def test_no_cache_and_failed_fetch_returns_none_meaning_candidate(tmp_path):
    session = FakeSession({"/hosts/domains": OSError("network down")})
    assert debrid.supported_domains(REAL_DEBRID, session=session) is None


def test_corrupt_cache_file_is_tolerated(tmp_path):
    (tmp_path / "debrid_hosts.json").write_text("{not json")
    session = FakeSession({"/hosts/domains": _Resp(["rapidgator.net"])})
    assert debrid.supported_domains(REAL_DEBRID, session=session) == ["rapidgator.net"]


# --------------------------------------------------------------------------
# Shared resolver
# --------------------------------------------------------------------------


def _ok_ad(link="https://s1.debrid.it/dl/a/f.zip", name="f.zip", size=100):
    return _Resp({"status": "success",
                  "data": {"link": link, "filename": name, "filesize": size}})


def _ok_rd(link="https://node-01.real-debrid.com/d/A/f.zip", name="f.zip", size=100):
    return _Resp({"download": link, "filename": name, "filesize": size})


def test_resolve_returns_none_when_no_provider_is_enabled():
    assert debrid.resolve("https://rapidgator.net/f", _settings()) is None


def test_resolve_returns_none_for_non_http_urls():
    settings = _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)
    assert debrid.resolve("magnet:?xt=urn:btih:abc", settings) is None
    assert debrid.resolve("ftp://rapidgator.net/f", settings) is None


def test_resolve_skips_provider_owned_urls():
    settings = _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)
    session = FakeSession({})
    assert debrid.resolve("https://s1.debrid.it/dl/a/f.zip", settings, session=session) is None
    assert debrid.resolve("https://real-debrid.com/d/ABC", settings, session=session) is None
    assert session.calls == []


def test_resolve_uses_the_only_enabled_provider(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    settings = _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)
    session = FakeSession({"/link/unlock": _ok_ad()})
    result = debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert result.provider == ALL_DEBRID


def test_resolve_prefers_alldebrid_when_both_enabled(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    _seed_cache(tmp_path, REAL_DEBRID, ["rapidgator.net"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
        debrid_preferred_provider="alldebrid",
    )
    session = FakeSession({"/link/unlock": _ok_ad(), "/unrestrict/link": _ok_rd()})
    result = debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert result.provider == ALL_DEBRID
    assert not any("/unrestrict/link" in url for _m, url, _k in session.calls)


def test_resolve_prefers_real_debrid_when_configured_first(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    _seed_cache(tmp_path, REAL_DEBRID, ["rapidgator.net"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
        debrid_preferred_provider="real_debrid",
    )
    session = FakeSession({"/link/unlock": _ok_ad(), "/unrestrict/link": _ok_rd()})
    result = debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert result.provider == REAL_DEBRID
    assert not any("/link/unlock" in url for _m, url, _k in session.calls)


def test_resolve_skips_a_provider_whose_cached_domains_exclude_the_host(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["other.example"])
    _seed_cache(tmp_path, REAL_DEBRID, ["rapidgator.net"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
    )
    session = FakeSession({"/unrestrict/link": _ok_rd()})
    result = debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert result.provider == REAL_DEBRID


def test_resolve_returns_none_when_no_provider_claims_the_host(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["other.example"])
    _seed_cache(tmp_path, REAL_DEBRID, ["another.example"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
    )
    session = FakeSession({})
    assert debrid.resolve("https://rapidgator.net/f", settings, session=session) is None


def test_resolve_returns_none_when_every_provider_reports_unsupported_host(tmp_path):
    """Both providers were candidates but rejected the host at resolve time:
    Cove must fall through to the plain direct download, not raise."""
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
    )
    session = FakeSession({
        "/hosts/domains": OSError("offline"),  # no cache -> everyone is a candidate
        "/link/unlock": _Resp({"status": "error",
                               "error": {"code": "LINK_HOST_NOT_SUPPORTED"}}),
        "/unrestrict/link": _Resp({"error": "unsupported_hoster", "error_code": 16}, 403),
    })
    assert debrid.resolve("https://weird.example/f", settings, session=session) is None


def test_resolve_falls_back_to_the_second_provider_on_allowed_failure(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    _seed_cache(tmp_path, REAL_DEBRID, ["rapidgator.net"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
    )
    session = FakeSession({
        "/link/unlock": _Resp({"status": "error", "error": {"code": "LINK_HOST_FULL"}}),
        "/unrestrict/link": _ok_rd(),
    })
    result = debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert result.provider == REAL_DEBRID


def test_resolve_does_not_fall_back_on_invalid_credentials(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    _seed_cache(tmp_path, REAL_DEBRID, ["rapidgator.net"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
    )
    session = FakeSession({
        "/link/unlock": _Resp({"status": "error", "error": {"code": "AUTH_BAD_APIKEY"}}),
        "/unrestrict/link": _ok_rd(),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert excinfo.value.provider == ALL_DEBRID
    assert not any("/unrestrict/link" in url for _m, url, _k in session.calls)


def test_resolve_does_not_fall_back_on_account_ban(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    _seed_cache(tmp_path, REAL_DEBRID, ["rapidgator.net"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
    )
    session = FakeSession({
        "/link/unlock": _Resp({"status": "error", "error": {"code": "AUTH_USER_BANNED"}}),
        "/unrestrict/link": _ok_rd(),
    })
    with pytest.raises(DebridError):
        debrid.resolve("https://rapidgator.net/f", settings, session=session)


def test_resolve_raises_when_an_enabled_provider_has_no_credential(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    settings = _settings(all_debrid_enabled=True, all_debrid_api_key="   ")
    with pytest.raises(DebridError) as excinfo:
        debrid.resolve("https://rapidgator.net/f", settings, session=FakeSession({}))
    assert excinfo.value.fallback_allowed is False
    assert excinfo.value.provider == ALL_DEBRID


def test_resolve_raises_the_last_error_when_all_fallbacks_are_exhausted(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    _seed_cache(tmp_path, REAL_DEBRID, ["rapidgator.net"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
    )
    session = FakeSession({
        "/link/unlock": _Resp({"status": "error", "error": {"code": "LINK_HOST_FULL"}}),
        "/unrestrict/link": _Resp({"error": "hoster_unavailable", "error_code": 17}, 403),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert excinfo.value.provider == REAL_DEBRID


def test_resolve_treats_unknown_domains_as_candidates_when_cache_is_unavailable(tmp_path):
    settings = _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)
    session = FakeSession({
        "/hosts/domains": OSError("offline"),
        "/link/unlock": _ok_ad(),
    })
    result = debrid.resolve("https://unknown.example/f", settings, session=session)
    assert result.provider == ALL_DEBRID


def test_resolve_surfaces_auth_errors_even_without_a_domain_cache(tmp_path):
    settings = _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)
    session = FakeSession({
        "/hosts/domains": OSError("offline"),
        "/link/unlock": _Resp({"status": "error", "error": {"code": "AUTH_BAD_APIKEY"}}),
    })
    with pytest.raises(DebridError):
        debrid.resolve("https://unknown.example/f", settings, session=session)


def test_invalid_preferred_provider_falls_back_to_alldebrid_ordering(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    _seed_cache(tmp_path, REAL_DEBRID, ["rapidgator.net"])
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
        debrid_preferred_provider="nonsense",
    )
    session = FakeSession({"/link/unlock": _ok_ad(), "/unrestrict/link": _ok_rd()})
    result = debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert result.provider == ALL_DEBRID


def test_is_enabled_tracks_the_provider_switches_not_the_credentials():
    assert debrid.is_enabled(_settings()) is False
    assert debrid.is_enabled(
        _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)) is True
    assert debrid.is_enabled(
        _settings(real_debrid_enabled=True, real_debrid_api_token=TOKEN)) is True
    # An enabled provider with no key must still route through the resolver
    # so the user sees the credential error instead of a silent free-tier
    # download from the hoster.
    assert debrid.is_enabled(_settings(all_debrid_enabled=True)) is True


# --------------------------------------------------------------------------
# Secret handling
# --------------------------------------------------------------------------


def _all_error_text(err):
    return " ".join([str(err), repr(err), err.user_message, str(err.args)])


def test_provider_errors_never_contain_credentials():
    cases = [
        (FakeSession({"/link/unlock": _Resp(
            {"status": "error", "error": {"code": "AUTH_BAD_APIKEY"}})}),
         lambda s: debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=s)),
        (FakeSession({"/unrestrict/link": _Resp(
            {"error": "bad_token", "error_code": 8}, 401)}),
         lambda s: debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=s)),
        (FakeSession({"/link/unlock": _Resp(ValueError("nope"))}),
         lambda s: debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=s)),
        (FakeSession({"/unrestrict/link": _Resp({}, 500)}),
         lambda s: debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=s)),
    ]
    for session, call in cases:
        with pytest.raises(DebridError) as excinfo:
            call(session)
        text = _all_error_text(excinfo.value)
        assert APIKEY not in text
        assert TOKEN not in text
        assert "SECRET" not in text


def test_errors_never_contain_the_generated_node_url():
    node = "https://node-01.real-debrid.com/d/SECRETNODE/file.zip"
    session = FakeSession({
        "/unrestrict/link": _Resp({"download": node, "filename": "f", "filesize": -1}),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)
    text = _all_error_text(excinfo.value)
    assert "SECRETNODE" not in text
    assert node not in text


def test_errors_never_echo_the_original_url_query_string():
    original = "https://rapidgator.net/file/1?auth=SECRETQUERY"
    session = FakeSession({
        "/link/unlock": _Resp({"status": "error", "error": {"code": "LINK_ERROR"}}),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_unrestrict(original, APIKEY, session=session)
    assert "SECRETQUERY" not in _all_error_text(excinfo.value)


def test_provider_supplied_message_is_not_echoed_verbatim():
    """A hostile or chatty provider message must not become the UI string."""
    session = FakeSession({
        "/link/unlock": _Resp({
            "status": "error",
            "error": {"code": "LINK_ERROR", "message": f"failed for key {APIKEY}"},
        }),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)
    assert APIKEY not in _all_error_text(excinfo.value)


def test_resolver_errors_are_sanitized(tmp_path):
    _seed_cache(tmp_path, ALL_DEBRID, ["rapidgator.net"])
    settings = _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)
    session = FakeSession({
        "/link/unlock": _Resp({"status": "error", "error": {"code": "AUTH_BAD_APIKEY"}}),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.resolve("https://rapidgator.net/f?token=SECRETQUERY", settings, session=session)
    text = _all_error_text(excinfo.value)
    assert APIKEY not in text
    assert "SECRETQUERY" not in text
    assert "AllDebrid" in str(excinfo.value)


def test_http_calls_always_pass_a_timeout():
    session = FakeSession({
        "/link/unlock": _ok_ad(),
        "/unrestrict/link": _ok_rd(),
        "/v4/user": _Resp({"status": "success", "data": {"user": {"username": "u"}}}),
        "/user": _Resp({"username": "u", "type": "premium"}),
        "/hosts/domains": _Resp(["rapidgator.net"]),
    })
    debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY, session=session)
    debrid.real_debrid_unrestrict("https://rapidgator.net/f", TOKEN, session=session)
    debrid.all_debrid_account(APIKEY, session=session)
    debrid.real_debrid_supported_domains(session=session)
    assert session.calls
    for _method, url, kwargs in session.calls:
        assert kwargs.get("timeout") is not None, url


# --------------------------------------------------------------------------
# Settings persistence
# --------------------------------------------------------------------------


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "settings.json")
    return tmp_path / "settings.json"


def test_debrid_settings_defaults_are_disabled_and_alldebrid_preferred():
    s = Settings()
    assert s.all_debrid_enabled is False
    assert s.all_debrid_api_key == ""
    assert s.real_debrid_enabled is False
    assert s.real_debrid_api_token == ""
    assert s.debrid_preferred_provider == "alldebrid"


def test_debrid_settings_round_trip(settings_env):
    s = Settings()
    s.all_debrid_enabled = True
    s.all_debrid_api_key = APIKEY
    s.real_debrid_enabled = True
    s.real_debrid_api_token = TOKEN
    s.debrid_preferred_provider = "real_debrid"
    s.save()

    loaded = Settings.load()
    assert loaded.all_debrid_enabled is True
    assert loaded.all_debrid_api_key == APIKEY
    assert loaded.real_debrid_enabled is True
    assert loaded.real_debrid_api_token == TOKEN
    assert loaded.debrid_preferred_provider == "real_debrid"


def test_debrid_settings_type_guards_reset_bad_values(settings_env):
    Settings().save()
    raw = json.loads(settings_env.read_text())
    raw.update({
        "all_debrid_enabled": "yes",
        "all_debrid_api_key": 12345,
        "real_debrid_enabled": 1,
        "real_debrid_api_token": None,
        "debrid_preferred_provider": "premiumize",
    })
    settings_env.write_text(json.dumps(raw))

    loaded = Settings.load()
    assert loaded.all_debrid_enabled is False
    assert loaded.all_debrid_api_key == ""
    assert loaded.real_debrid_enabled is False
    assert loaded.real_debrid_api_token == ""
    assert loaded.debrid_preferred_provider == "alldebrid"


def test_settings_file_keeps_restrictive_permissions(settings_env):
    import os
    import stat

    s = Settings()
    s.all_debrid_enabled = True
    s.all_debrid_api_key = APIKEY
    s.save()
    if os.name == "posix":
        mode = stat.S_IMODE(settings_env.stat().st_mode)
        assert mode == 0o600


# --------------------------------------------------------------------------
# Provider filenames are untrusted input
# --------------------------------------------------------------------------


def _unlocked_name(name):
    session = FakeSession({
        "/link/unlock": _Resp({"status": "success", "data": {
            "link": "https://s1.debrid.it/dl/a/f.bin", "filename": name, "filesize": 1}}),
    })
    return debrid.all_debrid_unrestrict("https://rapidgator.net/f", APIKEY,
                                        session=session).filename


def test_provider_filename_keeps_only_the_basename():
    assert _unlocked_name("../../etc/passwd") == "passwd"
    assert _unlocked_name(r"C:\Windows\System32\evil.exe") == "evil.exe"


@pytest.mark.parametrize(
    "name",
    [
        'movie:part.mp4',      # Windows-reserved character
        'movie?.mp4',
        'a<b>.mp4',
        'a"b.mp4',
        "a|b.mp4",
        "a*b.mp4",
        "CON",                 # Windows reserved device name
        "con.txt",
        "LPT1.mkv",
        "NUL.mp4",
        ".",
        "..",
        "",
        "   ",
        "x" * 256,             # too long
    ],
)
def test_unusable_provider_filenames_are_dropped(name):
    """Dropping the name lets aria2 name the file; the download still runs.
    The API path rejects these outright, so they must not slip in here."""
    assert _unlocked_name(name) == ""


@pytest.mark.parametrize(
    "name,expected",
    [
        ("movie.mkv", "movie.mkv"),
        ("  movie.mkv  ", "movie.mkv"),
        ("movie.mkv.", "movie.mkv"),      # trailing period is illegal on Windows
        ("movie.mkv...", "movie.mkv"),
        ("movie.mkv ", "movie.mkv"),
        ("a movie [1080p].mkv", "a movie [1080p].mkv"),
        ("Ordinateur - été.mkv", "Ordinateur - été.mkv"),
    ],
)
def test_usable_provider_filenames_are_preserved(name, expected):
    assert _unlocked_name(name) == expected


def test_accepted_provider_filenames_satisfy_coves_own_filename_rules():
    """Guard against the debrid rules drifting from api_server.validate_filename,
    which is the canonical definition and is intentionally not imported by
    cove.debrid (it would pull the HTTP server into a Qt-free module)."""
    from cove.api_server import ApiProblem, validate_filename

    candidates = [
        "movie.mkv", "  movie.mkv  ", "movie.mkv.", "a movie [1080p].mkv",
        "Ordinateur - été.mkv", "../../etc/passwd", r"C:\Windows\evil.exe",
        "movie:part.mp4", "movie?.mp4", "CON", "con.txt", "LPT1.mkv",
        ".", "..", "", "   ", "x" * 256, "a<b>.mp4", "a|b.mp4", "a*b.mp4",
        "file\x01name.mkv", "NUL.mp4", "AUX", "COM9.bin",
    ]
    for candidate in candidates:
        accepted = debrid._safe_filename(candidate)
        if not accepted:
            continue
        try:
            validate_filename(accepted)
        except ApiProblem as exc:
            raise AssertionError(
                f"debrid accepted {accepted!r} (from {candidate!r}) but Cove's "
                f"own filename validation rejects it: {exc.message}"
            ) from None


# --------------------------------------------------------------------------
# Provider share / landing links
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected_label",
    [
        ("https://real-debrid.com/d/ALJRILITCGUEW127", "Real-Debrid"),
        ("https://www.real-debrid.com/d/ABC123", "Real-Debrid"),
        ("http://real-debrid.com/d/ABC123", "Real-Debrid"),
        ("https://REAL-DEBRID.com/d/ABC123", "Real-Debrid"),
        ("https://alldebrid.com/f/XYZ789", "AllDebrid"),
        ("https://www.alldebrid.com/f/XYZ789", "AllDebrid"),
    ],
)
def test_account_bound_share_links_are_identified(url, expected_label):
    reason = debrid.share_link_reason(url)
    assert reason
    assert expected_label in reason
    assert "original" in reason.lower()


@pytest.mark.parametrize(
    "url",
    [
        # Generated node URLs. These are plain direct links that do work when
        # pasted by hand, so they must keep downloading normally.
        "https://s1.debrid.it/dl/abc/file.zip",
        "https://debrid.it/dl/abc/file.zip",
        "https://45.download.real-debrid.com/d/ABC123/file.zip",
        "https://node-01.real-debrid.com/d/ABC/file.zip",
        # Not a share path.
        "https://real-debrid.com/",
        "https://real-debrid.com/premium",
        "https://alldebrid.com/",
        "https://alldebrid.com/pricing",
        "https://real-debrid.com/f/ABC",
        "https://alldebrid.com/d/ABC",
        # Lookalike hosts must not be claimed.
        "https://real-debrid.com.evil.test/d/ABC",
        "https://notreal-debrid.com/d/ABC",
        # Ordinary hosters and non-http input.
        "https://rapidgator.net/file/abc",
        "magnet:?xt=urn:btih:abc",
        "",
        None,
    ],
)
def test_non_share_links_are_left_alone(url):
    assert debrid.share_link_reason(url) == ""


# ---------------------------------------------------------------------------
# Cached torrents
# ---------------------------------------------------------------------------
#
# Two things must never happen on this route: Cove sitting through a
# provider cloud-download and calling the result "cached", and Cove leaving
# probe entries behind in the user's provider account.

INFO_HASH = "0123456789abcdef0123456789abcdef01234567"
MINIMAL_MAGNET = f"magnet:?xt=urn:btih:{INFO_HASH}"
# A tracker passkey that must never reach a provider.
TRACKER_MAGNET = (
    f"magnet:?xt=urn:btih:{INFO_HASH}&dn=Season+1"
    "&tr=http://tracker.example/announce?passkey=SECRETPASS"
)
AD_LOCKED_1 = "https://alldebrid.com/f/LOCKEDONE"
AD_LOCKED_2 = "https://alldebrid.com/f/LOCKEDTWO"
RD_LOCKED_1 = "https://real-debrid.com/d/LOCKEDONE"
RD_LOCKED_2 = "https://real-debrid.com/d/LOCKEDTWO"


def _ad_upload(ready=True, name="Season 1", magnet_id=42):
    return _Resp({"status": "success", "data": {"magnets": [
        {"magnet": MINIMAL_MAGNET, "hash": INFO_HASH, "name": name,
         "size": 30, "ready": ready, "id": magnet_id},
    ]}})


def _ad_upload_file(ready=True, name="Season 1", magnet_id=42):
    return _Resp({"status": "success", "data": {"files": [
        {"file": "cove.torrent", "hash": INFO_HASH, "name": name,
         "size": 30, "ready": ready, "id": magnet_id},
    ]}})


def _ad_files(tree=None):
    if tree is None:
        tree = [{"n": "Season 1", "e": [
            {"n": "ep1.mkv", "s": 10, "l": AD_LOCKED_1},
            {"n": "extras", "e": [{"n": "ep2.mkv", "s": 20, "l": AD_LOCKED_2}]},
        ]}]
    return _Resp({"status": "success", "data": {"magnets": [
        {"id": 42, "files": tree},
    ]}})


_AD_DELETED = _Resp({"status": "success", "data": {"message": "deleted"}})


def _ad_calls(session, suffix):
    return [c for c in session.calls if c[1].endswith(suffix)]


def test_all_debrid_cached_magnet_returns_the_file_tree():
    session = FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _ad_files(),
    })
    cached = debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session)

    assert cached.provider == ALL_DEBRID
    assert cached.info_hash == INFO_HASH
    assert cached.name == "Season 1"
    assert [f.path for f in cached.files] == [("ep1.mkv",), ("extras", "ep2.mkv")]
    assert [f.size for f in cached.files] == [10, 20]
    assert [f.locked_link for f in cached.files] == [AD_LOCKED_1, AD_LOCKED_2]
    # The tree's own root folder is dropped; the queue adds it back once.
    assert cached.multi_file is True
    assert cached.destination_parts(cached.files[1]) == ("Season 1", "extras", "ep2.mkv")
    # A cached entry stays: its links are what the download uses.
    assert _ad_calls(session, "/magnet/delete") == []


def test_all_debrid_uncached_magnet_returns_none_and_deletes_the_entry():
    session = FakeSession({
        "/magnet/upload": _ad_upload(ready=False),
        "/magnet/delete": _AD_DELETED,
    })
    assert debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session) is None
    assert len(_ad_calls(session, "/magnet/delete")) == 1
    # Cove never waits for AllDebrid to fetch it.
    assert _ad_calls(session, "/magnet/files") == []


def test_all_debrid_only_ever_receives_the_minimal_magnet():
    session = FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _ad_files(),
    })
    parsed = debrid._torrent.parse_magnet(TRACKER_MAGNET)
    debrid.all_debrid_cached_torrent(parsed.info_hash, APIKEY, session=session)

    sent = repr(session.calls)
    assert MINIMAL_MAGNET in sent
    for leaked in ("SECRETPASS", "tracker.example", "dn=", "Season+1"):
        assert leaked not in sent


def test_all_debrid_torrent_file_upload_uses_multipart():
    session = FakeSession({
        "/magnet/upload/file": _ad_upload_file(),
        "/magnet/files": _ad_files(),
    })
    cached = debrid.all_debrid_cached_torrent(
        INFO_HASH, APIKEY, torrent_bytes=b"d4:infod1:xi1eee", session=session
    )
    assert cached is not None
    upload = _ad_calls(session, "/magnet/upload/file")[0]
    assert "files" in upload[2]


def test_all_debrid_torrent_file_not_ready_is_deleted():
    session = FakeSession({
        "/magnet/upload/file": _ad_upload_file(ready=False),
        "/magnet/delete": _AD_DELETED,
    })
    assert debrid.all_debrid_cached_torrent(
        INFO_HASH, APIKEY, torrent_bytes=b"x", session=session
    ) is None
    assert len(_ad_calls(session, "/magnet/delete")) == 1


def test_all_debrid_malformed_file_tree_deletes_the_entry():
    session = FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _Resp({"status": "success", "data": {"magnets": "nope"}}),
        "/magnet/delete": _AD_DELETED,
    })
    with pytest.raises(DebridError):
        debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session)
    assert len(_ad_calls(session, "/magnet/delete")) == 1


def test_all_debrid_missing_locked_link_deletes_the_entry():
    session = FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _ad_files([{"n": "ep1.mkv", "s": 10}]),
        "/magnet/delete": _AD_DELETED,
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session)
    assert excinfo.value.code == "missing_link"
    assert len(_ad_calls(session, "/magnet/delete")) == 1


def test_all_debrid_unsafe_provider_path_is_refused_and_cleaned_up():
    session = FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _ad_files([{"n": "../escape.bin", "s": 1, "l": AD_LOCKED_1}]),
        "/magnet/delete": _AD_DELETED,
    })
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session)
    assert excinfo.value.code == "unsafe_path"
    assert len(_ad_calls(session, "/magnet/delete")) == 1


def test_all_debrid_transport_failure_after_creation_still_deletes():
    session = FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": RuntimeError("boom"),
        "/magnet/delete": _AD_DELETED,
    })
    with pytest.raises(DebridError):
        debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session)
    assert len(_ad_calls(session, "/magnet/delete")) == 1


def test_all_debrid_delete_retries_once_then_gives_up():
    session = FakeSession({
        "/magnet/upload": _ad_upload(ready=False),
        "/magnet/delete": RuntimeError("boom"),
    })
    assert debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session) is None
    # One retry for a transport blip, then it stops. Never an unbounded loop.
    assert len(_ad_calls(session, "/magnet/delete")) == 2


def test_all_debrid_delete_is_not_retried_for_a_refusal():
    refused = _Resp({"status": "error", "error": {"code": "AUTH_BAD_APIKEY"}})
    session = FakeSession({
        "/magnet/upload": _ad_upload(ready=False),
        "/magnet/delete": refused,
    })
    assert debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session) is None
    assert len(_ad_calls(session, "/magnet/delete")) == 1


def test_all_debrid_single_file_torrent_keeps_a_flat_destination():
    session = FakeSession({
        "/magnet/upload": _ad_upload(name="movie.mkv"),
        "/magnet/files": _ad_files([{"n": "movie.mkv", "s": 7, "l": AD_LOCKED_1}]),
    })
    cached = debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session)
    assert cached.multi_file is False
    assert cached.destination_parts(cached.files[0]) == ("movie.mkv",)


# --- Real-Debrid -----------------------------------------------------------


def _rd_added(torrent_id="rd-1"):
    return _Resp({"id": torrent_id, "uri": "/torrents/info/rd-1"}, status_code=201)


def _rd_info(status, files=None, links=None, filename="Season 1"):
    if files is None:
        files = [
            {"id": 1, "path": "/ep1.mkv", "bytes": 10, "selected": 1},
            {"id": 2, "path": "/extras/ep2.mkv", "bytes": 20, "selected": 1},
        ]
    if links is None:
        links = [RD_LOCKED_1, RD_LOCKED_2]
    return _Resp({
        "id": "rd-1", "filename": filename, "status": status,
        "files": files, "links": links,
    })


_RD_NO_CONTENT = _Resp(ValueError("no body"), status_code=204)


def _rd_calls(session, suffix):
    return [c for c in session.calls if c[1].endswith(suffix)]


def _rd_env(info_responses, **extra):
    routes = {
        "/torrents/addMagnet": _rd_added(),
        "/torrents/info/rd-1": list(info_responses),
        "/torrents/selectFiles/rd-1": _RD_NO_CONTENT,
        "/torrents/delete/rd-1": _RD_NO_CONTENT,
    }
    routes.update(extra)
    return FakeSession(routes)


def test_real_debrid_cached_magnet_returns_the_file_tree():
    session = _rd_env([_rd_info("downloaded")])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached.provider == REAL_DEBRID
    assert cached.name == "Season 1"
    assert [f.path for f in cached.files] == [("ep1.mkv",), ("extras", "ep2.mkv")]
    assert [f.locked_link for f in cached.files] == [RD_LOCKED_1, RD_LOCKED_2]
    assert clock.slept == []
    # A cached entry is kept: the download uses its links.
    assert _rd_calls(session, "/torrents/delete/rd-1") == []


def test_real_debrid_only_ever_receives_the_minimal_magnet():
    session = _rd_env([_rd_info("downloaded")])
    clock = FakeClock()
    parsed = debrid._torrent.parse_magnet(TRACKER_MAGNET)
    debrid.real_debrid_cached_torrent(
        parsed.info_hash, TOKEN, session=session,
        sleep=clock.sleep, clock=clock.monotonic,
    )
    sent = repr(session.calls)
    assert MINIMAL_MAGNET in sent
    for leaked in ("SECRETPASS", "tracker.example", "Season+1"):
        assert leaked not in sent


def test_real_debrid_waits_only_for_magnet_conversion():
    session = _rd_env([
        _rd_info("magnet_conversion"),
        _rd_info("magnet_conversion"),
        _rd_info("downloaded"),
    ])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached is not None
    assert clock.slept == [debrid.RD_TORRENT_POLL_INTERVAL] * 2


def test_real_debrid_conversion_timeout_is_uncached_and_cleaned_up():
    session = _rd_env([_rd_info("magnet_conversion")] * 40)
    clock = FakeClock()
    assert debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    ) is None
    assert sum(clock.slept) <= debrid.RD_TORRENT_MAX_WAIT + debrid.RD_TORRENT_POLL_INTERVAL
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_selects_all_files_then_rechecks():
    session = _rd_env([_rd_info("waiting_files_selection"), _rd_info("downloaded")])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached is not None
    select = _rd_calls(session, "/torrents/selectFiles/rd-1")
    assert len(select) == 1
    assert select[0][2]["data"] == {"files": "all"}


def test_real_debrid_repeated_selection_state_is_not_a_loop():
    session = _rd_env([_rd_info("waiting_files_selection")] * 5)
    clock = FakeClock()
    assert debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    ) is None
    assert len(_rd_calls(session, "/torrents/selectFiles/rd-1")) == 1
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


@pytest.mark.parametrize("status", [
    "queued", "downloading", "compressing", "uploading",
    "error", "virus", "dead", "magnet_error",
])
def test_real_debrid_non_cached_states_return_none_and_delete(status):
    session = _rd_env([_rd_info(status)])
    clock = FakeClock()
    assert debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    ) is None
    assert clock.slept == []
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_never_waits_for_a_cloud_download_to_finish():
    """Downloading first, downloaded later, must still be "not cached"."""
    session = _rd_env([_rd_info("downloading"), _rd_info("downloaded")])
    clock = FakeClock()
    assert debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    ) is None


def test_real_debrid_link_count_mismatch_is_refused_and_cleaned_up():
    session = _rd_env([_rd_info("downloaded", links=[RD_LOCKED_1])])
    clock = FakeClock()
    with pytest.raises(DebridError):
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_unselected_files_are_ignored():
    session = _rd_env([_rd_info("downloaded", files=[
        {"id": 1, "path": "/ep1.mkv", "bytes": 10, "selected": 1},
        {"id": 2, "path": "/sample.mkv", "bytes": 1, "selected": 0},
    ], links=[RD_LOCKED_1])])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert [f.path for f in cached.files] == [("ep1.mkv",)]


def test_real_debrid_unsafe_provider_path_is_refused_and_cleaned_up():
    session = _rd_env([_rd_info("downloaded", files=[
        {"id": 1, "path": "/../escape.bin", "bytes": 1, "selected": 1},
    ], links=[RD_LOCKED_1])])
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    assert excinfo.value.code == "unsafe_path"
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_exception_during_parsing_still_deletes():
    session = _rd_env([_Resp({"id": "rd-1", "status": 5})])
    clock = FakeClock()
    with pytest.raises(DebridError):
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_delete_retries_once_then_gives_up():
    session = _rd_env([_rd_info("queued")], **{"/torrents/delete/rd-1": RuntimeError("boom")})
    clock = FakeClock()
    assert debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    ) is None
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 2


def test_real_debrid_torrent_file_uploads_raw_bytes():
    session = FakeSession({
        "/torrents/addTorrent": _rd_added(),
        "/torrents/info/rd-1": [_rd_info("downloaded")],
        "/torrents/delete/rd-1": _RD_NO_CONTENT,
    })
    clock = FakeClock()
    raw = b"d4:infod1:xi1eee"
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, torrent_bytes=raw, session=session,
        sleep=clock.sleep, clock=clock.monotonic,
    )
    assert cached is not None
    put = _rd_calls(session, "/torrents/addTorrent")[0]
    assert put[0] == "PUT"
    assert put[2]["data"] == raw


# --- Shared torrent routing ------------------------------------------------


def _torrent_settings(**extra):
    base = dict(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
        debrid_preferred_provider="alldebrid",
    )
    base.update(extra)
    return _settings(**base)


def _both_providers_session(ad_ready, rd_status):
    return FakeSession({
        "/magnet/upload": _ad_upload(ready=ad_ready),
        "/magnet/files": _ad_files(),
        "/magnet/delete": _AD_DELETED,
        "/torrents/addMagnet": _rd_added(),
        "/torrents/info/rd-1": [_rd_info(rd_status)],
        "/torrents/delete/rd-1": _RD_NO_CONTENT,
    })


def _resolve(settings, session):
    clock = FakeClock()
    return debrid.resolve_torrent(
        INFO_HASH, settings, session=session,
        sleep=clock.sleep, clock=clock.monotonic,
    )


def test_resolve_torrent_prefers_all_debrid():
    session = _both_providers_session(True, "downloaded")
    cached = _resolve(_torrent_settings(), session)
    assert cached.provider == ALL_DEBRID
    assert _rd_calls(session, "/torrents/addMagnet") == []


def test_resolve_torrent_prefers_real_debrid_when_asked():
    session = _both_providers_session(True, "downloaded")
    cached = _resolve(_torrent_settings(debrid_preferred_provider="real_debrid"), session)
    assert cached.provider == REAL_DEBRID
    assert _ad_calls(session, "/magnet/upload") == []


def test_resolve_torrent_falls_through_to_the_second_provider():
    session = _both_providers_session(False, "downloaded")
    cached = _resolve(_torrent_settings(), session)
    assert cached.provider == REAL_DEBRID
    # The uncached AllDebrid probe entry is still cleaned up.
    assert len(_ad_calls(session, "/magnet/delete")) == 1


def test_resolve_torrent_returns_none_when_neither_has_it():
    session = _both_providers_session(False, "queued")
    assert _resolve(_torrent_settings(), session) is None
    assert len(_ad_calls(session, "/magnet/delete")) == 1
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_resolve_torrent_with_one_provider_enabled():
    session = FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _ad_files(),
    })
    settings = _torrent_settings(real_debrid_enabled=False, real_debrid_api_token="")
    assert _resolve(settings, session).provider == ALL_DEBRID


def test_resolve_torrent_with_no_provider_configured_returns_none():
    session = FakeSession({})
    assert _resolve(_settings(), session) is None
    assert session.calls == []


def test_resolve_torrent_falls_back_past_a_temporary_provider_failure():
    session = FakeSession({
        "/magnet/upload": _Resp({"status": "error", "error": {"code": "MAINTENANCE"}}),
        "/torrents/addMagnet": _rd_added(),
        "/torrents/info/rd-1": [_rd_info("downloaded")],
    })
    assert _resolve(_torrent_settings(), session).provider == REAL_DEBRID


def test_resolve_torrent_does_not_hide_a_credential_failure():
    session = FakeSession({
        "/magnet/upload": _Resp({"status": "error", "error": {"code": "AUTH_BAD_APIKEY"}}),
    })
    with pytest.raises(DebridError) as excinfo:
        _resolve(_torrent_settings(), session)
    assert excinfo.value.provider == ALL_DEBRID
    # Never silently retried against the other provider.
    assert "/torrents/addMagnet" not in repr(session.calls)


def test_resolve_torrent_reports_a_missing_credential():
    settings = _torrent_settings(all_debrid_api_key="  ")
    with pytest.raises(DebridError) as excinfo:
        _resolve(settings, FakeSession({}))
    assert excinfo.value.code == "missing_credential"


def test_resolve_torrent_raises_the_last_error_when_all_fall_back():
    session = FakeSession({
        "/magnet/upload": _Resp({"status": "error", "error": {"code": "MAINTENANCE"}}),
        "/torrents/addMagnet": _Resp({"error_code": 25}),
    })
    with pytest.raises(DebridError) as excinfo:
        _resolve(_torrent_settings(), session)
    assert excinfo.value.provider == REAL_DEBRID


def test_resolve_torrent_uses_pure_provider_identifiers():
    session = _both_providers_session(True, "downloaded")
    assert _resolve(_torrent_settings(), session).provider in config.DEBRID_PROVIDERS


# --- Unlocking a stored torrent file link ----------------------------------


def test_unlock_torrent_file_uses_the_recorded_provider():
    session = FakeSession({"/link/unlock": _Resp({"status": "success", "data": {
        "link": "https://s1.debrid.it/dl/NODE/ep1.mkv",
        "filename": "ep1.mkv", "filesize": 10,
    }})})
    result = debrid.unlock_torrent_file(
        AD_LOCKED_1, ALL_DEBRID, _torrent_settings(), session=session
    )
    assert result.provider == ALL_DEBRID
    assert result.download == "https://s1.debrid.it/dl/NODE/ep1.mkv"
    assert session.calls[0][2]["data"] == {"link": AD_LOCKED_1}


def test_unlock_torrent_file_reports_a_missing_credential():
    settings = _torrent_settings(real_debrid_api_token="")
    with pytest.raises(DebridError) as excinfo:
        debrid.unlock_torrent_file(RD_LOCKED_1, REAL_DEBRID, settings, session=FakeSession({}))
    assert excinfo.value.code == "missing_credential"


def test_unlock_torrent_file_rejects_an_unknown_provider():
    with pytest.raises(DebridError):
        debrid.unlock_torrent_file(RD_LOCKED_1, "nope", _torrent_settings(), session=FakeSession({}))


# --- Leak checks -----------------------------------------------------------


@pytest.mark.parametrize("session,kwargs", [
    (FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _ad_files([{"n": "ep1.mkv", "s": 10}]),
        "/magnet/delete": _AD_DELETED,
    }), {}),
    (FakeSession({
        "/magnet/upload": _Resp({"status": "error", "error": {"code": "AUTH_BAD_APIKEY"}}),
    }), {}),
])
def test_torrent_errors_never_carry_secrets(session, kwargs):
    with pytest.raises(DebridError) as excinfo:
        debrid.all_debrid_cached_torrent(INFO_HASH, APIKEY, session=session, **kwargs)
    text = f"{excinfo.value} {excinfo.value.user_message}"
    for leaked in ("SECRET", APIKEY, TOKEN, INFO_HASH, AD_LOCKED_1, "LOCKED"):
        assert leaked not in text


def test_real_debrid_torrent_errors_never_carry_secrets():
    session = _rd_env([_rd_info("downloaded", links=[RD_LOCKED_1])])
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    text = f"{excinfo.value} {excinfo.value.user_message}"
    for leaked in ("SECRET", TOKEN, INFO_HASH, RD_LOCKED_1, "rd-1"):
        assert leaked not in text
