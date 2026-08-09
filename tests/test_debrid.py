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
        bare_url = url.split("?", 1)[0]
        for suffix, value in self.routes.items():
            if url.endswith(suffix) or bare_url.endswith(suffix):
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


@pytest.mark.parametrize(
    "settings",
    [None, _settings(), _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)],
)
def test_share_link_reason_ad_f_link_is_rejected_regardless_of_settings(settings):
    reason = debrid.share_link_reason("https://alldebrid.com/f/XYZ789", settings)
    assert reason
    assert "AllDebrid" in reason


@pytest.mark.parametrize(
    "settings",
    [None, _settings(), _settings(all_debrid_enabled=True, all_debrid_api_key=APIKEY)],
)
def test_share_link_reason_rd_d_link_is_rejected_when_rd_disabled(settings):
    reason = debrid.share_link_reason("https://real-debrid.com/d/ALJRILITCGUEW127", settings)
    assert reason
    assert "Real-Debrid" in reason


def test_share_link_reason_rd_d_link_is_allowed_when_rd_enabled():
    settings = _settings(real_debrid_enabled=True, real_debrid_api_token=TOKEN)
    assert debrid.share_link_reason("https://real-debrid.com/d/ALJRILITCGUEW127", settings) == ""


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://real-debrid.com/d/ABC", True),
        ("https://real-debrid.com/d/ABC/file.mp4", True),
        ("https://www.real-debrid.com/d/ABC", True),
        ("http://real-debrid.com/d/ABC", True),
        ("https://REAL-DEBRID.com/d/ABC", True),
        ("https://real-debrid.com/d/ABC?x=1", True),
        # Delivery subdomains are not share links.
        ("https://sgp.download.real-debrid.com/d/ABC/file.mp4", False),
        ("https://node-01.real-debrid.com/d/ABC/file.zip", False),
        # Wrong path, lookalike hosts, non-http.
        ("https://real-debrid.com/f/ABC", False),
        ("https://real-debrid.com/premium", False),
        ("https://real-debrid.com.evil.test/d/ABC", False),
        ("https://rapidgator.net/d/ABC", False),
        ("", False),
        (None, False),
        ("magnet:?xt=urn:btih:abc", False),
    ],
)
def test_is_real_debrid_share_link_matches_only_apex(url, expected):
    assert debrid.is_real_debrid_share_link(url) is expected


def test_resolve_unrestricts_an_rd_share_link_when_rd_is_configured():
    settings = _settings(real_debrid_enabled=True, real_debrid_api_token=TOKEN)
    session = FakeSession({"/unrestrict/link": _ok_rd()})
    result = debrid.resolve(
        "https://real-debrid.com/d/ALJRILITCGUEW127", settings, session=session
    )
    assert result.provider == REAL_DEBRID
    assert result.download == "https://node-01.real-debrid.com/d/A/f.zip"
    assert len(session.calls) == 1
    assert session.calls[0][1].endswith("/unrestrict/link")


def test_resolve_returns_none_for_an_rd_share_link_when_rd_is_disabled():
    settings = _settings()
    session = FakeSession({})
    assert debrid.resolve(
        "https://real-debrid.com/d/ALJRILITCGUEW127", settings, session=session
    ) is None
    assert session.calls == []


def test_resolve_raises_missing_credential_for_rd_share_link_when_rd_enabled_but_token_empty():
    settings = _settings(real_debrid_enabled=True)
    session = FakeSession({})
    with pytest.raises(DebridError) as excinfo:
        debrid.resolve(
            "https://real-debrid.com/d/ALJRILITCGUEW127", settings, session=session
        )
    assert excinfo.value.provider == REAL_DEBRID
    assert excinfo.value.code == "missing_credential"
    assert excinfo.value.fallback_allowed is False
    assert session.calls == []


def test_resolve_routes_only_to_rd_even_when_ad_is_also_enabled():
    settings = _settings(
        all_debrid_enabled=True,
        all_debrid_api_key=APIKEY,
        real_debrid_enabled=True,
        real_debrid_api_token=TOKEN,
        debrid_preferred_provider="alldebrid",
    )
    session = FakeSession({"/unrestrict/link": _ok_rd()})
    result = debrid.resolve(
        "https://real-debrid.com/d/ALJRILITCGUEW127", settings, session=session
    )
    assert result.provider == REAL_DEBRID
    assert all(call[1].endswith("/unrestrict/link") for call in session.calls)


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


def _rd_selected(count, prefix="ep"):
    """`count` selected files, all at the torrent root."""
    return [
        {"id": i + 1, "path": f"/{prefix}{i + 1}.mkv", "bytes": 10, "selected": 1}
        for i in range(count)
    ]


RD_PACKED_LINK = "https://real-debrid.com/d/PACKEDONE"
RD_PACKED_DELIVERY = "https://sgp.download.real-debrid.com/d/PACKEDONE/Season+1.rar"


def _rd_unrestricted(filename="Season 1.rar", filesize=1234):
    return _Resp({
        "download": RD_PACKED_DELIVERY,
        "filename": filename,
        "filesize": filesize,
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


def test_real_debrid_sleeps_before_the_first_post_selection_recheck():
    session = _rd_env([_rd_info("waiting_files_selection"), _rd_info("downloaded")])
    clock = FakeClock()
    debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert clock.slept == [debrid.RD_TORRENT_POLL_INTERVAL]
    # Order: info -> selectFiles -> info, with the sleep in between.
    paths = [c[1].rsplit("/api", 1)[-1] for c in session.calls]
    assert paths[1].endswith("/torrents/info/rd-1")
    assert paths[2].endswith("/torrents/selectFiles/rd-1")
    assert paths[3].endswith("/torrents/info/rd-1")


def test_real_debrid_repeated_selection_state_settles_into_a_cached_result():
    session = _rd_env([
        _rd_info("waiting_files_selection"),
        _rd_info("waiting_files_selection"),
        _rd_info("downloaded"),
    ])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached is not None
    assert len(cached.files) == 2
    assert len(_rd_calls(session, "/torrents/selectFiles/rd-1")) == 1
    assert _rd_calls(session, "/torrents/delete/rd-1") == []
    assert clock.slept == [debrid.RD_TORRENT_POLL_INTERVAL] * 2


def test_real_debrid_delayed_links_settle_into_a_cached_result():
    session = _rd_env([
        _rd_info("waiting_files_selection"),
        _rd_info("downloaded", links=[RD_LOCKED_1]),
        _rd_info("downloaded"),
    ])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached is not None
    assert [f.locked_link for f in cached.files] == [RD_LOCKED_1, RD_LOCKED_2]
    assert len(_rd_calls(session, "/torrents/selectFiles/rd-1")) == 1
    assert _rd_calls(session, "/torrents/delete/rd-1") == []


def test_real_debrid_selection_that_never_settles_is_uncached_and_cleaned_up():
    session = _rd_env([_rd_info("waiting_files_selection")] * 40)
    clock = FakeClock()
    assert debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    ) is None
    assert len(_rd_calls(session, "/torrents/selectFiles/rd-1")) == 1
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1
    assert sum(clock.slept) <= (
        debrid.RD_SELECTION_SETTLE_WAIT + debrid.RD_TORRENT_POLL_INTERVAL
    )


def test_real_debrid_links_that_never_settle_are_refused_and_cleaned_up():
    # Three selected files with two links is a partial mismatch: it is neither
    # a positional mapping nor the one-packed-link shape, so it stays refused.
    session = _rd_env(
        [_rd_info("waiting_files_selection", files=_rd_selected(3))]
        + [_rd_info(
            "downloaded", files=_rd_selected(3), links=[RD_LOCKED_1, RD_LOCKED_2],
        )] * 40
    )
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    assert excinfo.value.code == "bad_response"
    assert excinfo.value.user_message == "the response could not be understood."
    assert len(_rd_calls(session, "/torrents/selectFiles/rd-1")) == 1
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_selection_error_is_mapped_and_stops_the_probe():
    session = _rd_env(
        [_rd_info("waiting_files_selection")] * 5,
        **{"/torrents/selectFiles/rd-1": _Resp(
            {"error": "infringing_file", "error_code": 35}, status_code=403
        )},
    )
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    err = excinfo.value
    assert err.code == 35
    assert err.user_message == "the file was refused as infringing."
    assert err.fallback_allowed is False
    # No settling poll happens after a failed selection.
    assert len(_rd_calls(session, "/torrents/info/rd-1")) == 1
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
    session = _rd_env([_rd_info(
        "downloaded", files=_rd_selected(3), links=[RD_LOCKED_1, RD_LOCKED_2],
    )])
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


# --- Real-Debrid packed multi-file torrents ---------------------------------
#
# Real-Debrid may compress a cached multi-file torrent into a single packed
# link. The selected-file count then no longer matches the link count, and
# the one link is the whole torrent rather than one of its files.

_PACKED_MESSAGE = (
    "Real-Debrid returned this torrent as a packed file, but Cove could not "
    "determine a safe filename for it."
)


def test_real_debrid_packed_link_after_settling_becomes_one_file():
    session = _rd_env(
        [_rd_info("waiting_files_selection", files=_rd_selected(6))]
        + [_rd_info(
            "downloaded", files=_rd_selected(6), links=[RD_PACKED_LINK],
        )] * 40,
        **{"/unrestrict/link": _rd_unrestricted()},
    )
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached is not None
    # The full settling window is respected before the packed verdict.
    assert sum(clock.slept) >= debrid.RD_SELECTION_SETTLE_WAIT
    assert len(_rd_calls(session, "/torrents/selectFiles/rd-1")) == 1
    assert len(_rd_calls(session, "/unrestrict/link")) == 1
    assert len(cached.files) == 1
    assert cached.files[0].index == 0
    assert cached.files[0].path == ("Season 1.rar",)
    assert cached.files[0].size == 1234
    assert cached.files[0].locked_link == RD_PACKED_LINK
    # The temporary delivery URL is never carried into the result.
    assert RD_PACKED_DELIVERY not in repr(cached)
    assert _rd_calls(session, "/torrents/delete/rd-1") == []


def test_real_debrid_already_downloaded_packed_link_needs_no_settling():
    session = _rd_env(
        [_rd_info("downloaded", files=_rd_selected(50), links=[RD_PACKED_LINK])],
        **{"/unrestrict/link": _rd_unrestricted()},
    )
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached is not None
    assert clock.slept == []
    assert _rd_calls(session, "/torrents/selectFiles/rd-1") == []
    assert len(_rd_calls(session, "/unrestrict/link")) == 1
    assert len(cached.files) == 1
    assert cached.files[0].locked_link == RD_PACKED_LINK
    # One packed file is a flat destination, not a torrent wrapper folder.
    assert cached.multi_file is False
    assert cached.destination_parts(cached.files[0]) == ("Season 1.rar",)


def test_real_debrid_packed_filename_is_sanitized():
    session = _rd_env(
        [_rd_info("downloaded", files=_rd_selected(6), links=[RD_PACKED_LINK])],
        **{"/unrestrict/link": _rd_unrestricted(filename="../../evil/Season 1.rar")},
    )
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached.files[0].path == ("Season 1.rar",)


def test_real_debrid_packed_link_without_a_usable_filename_is_refused():
    session = _rd_env(
        [_rd_info("downloaded", files=_rd_selected(6), links=[RD_PACKED_LINK])],
        **{"/unrestrict/link": _rd_unrestricted(filename="   ")},
    )
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    err = excinfo.value
    assert err.code == "packed_unsupported"
    assert err.user_message == _PACKED_MESSAGE
    assert err.fallback_allowed is False
    assert RD_PACKED_LINK not in str(err)
    assert RD_PACKED_DELIVERY not in str(err)
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_packed_unrestrict_error_stays_the_primary_failure():
    session = _rd_env(
        [_rd_info("downloaded", files=_rd_selected(6), links=[RD_PACKED_LINK])],
        **{"/unrestrict/link": _Resp(
            {"error": "infringing_file", "error_code": 35}, status_code=403
        )},
    )
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    err = excinfo.value
    assert err.code == 35
    assert err.user_message == "the file was refused as infringing."
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_partial_link_mismatch_is_not_treated_as_packed():
    session = _rd_env(
        [_rd_info("waiting_files_selection", files=_rd_selected(4))]
        + [_rd_info(
            "downloaded", files=_rd_selected(4), links=[RD_LOCKED_1, RD_LOCKED_2],
        )] * 40
    )
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    assert excinfo.value.code == "bad_response"
    assert _rd_calls(session, "/unrestrict/link") == []
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_zero_links_is_not_treated_as_packed():
    session = _rd_env(
        [_rd_info("waiting_files_selection", files=_rd_selected(4))]
        + [_rd_info("downloaded", files=_rd_selected(4), links=[])] * 40
    )
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    assert excinfo.value.code == "bad_response"
    assert _rd_calls(session, "/unrestrict/link") == []
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_excess_links_are_not_treated_as_packed():
    session = _rd_env([_rd_info(
        "downloaded", files=_rd_selected(1),
        links=[RD_LOCKED_1, RD_LOCKED_2],
    )])
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    assert excinfo.value.code == "bad_response"
    assert _rd_calls(session, "/unrestrict/link") == []
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 1


def test_real_debrid_matching_multi_file_response_is_unchanged():
    session = _rd_env([_rd_info("downloaded")])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert [f.path for f in cached.files] == [("ep1.mkv",), ("extras", "ep2.mkv")]
    assert [f.locked_link for f in cached.files] == [RD_LOCKED_1, RD_LOCKED_2]
    assert _rd_calls(session, "/unrestrict/link") == []


def test_real_debrid_single_file_response_is_never_packed():
    session = _rd_env([_rd_info(
        "downloaded", files=_rd_selected(1, prefix="movie"), links=[RD_LOCKED_1],
    )])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert [f.path for f in cached.files] == [("movie1.mkv",)]
    assert cached.files[0].size == 10
    assert _rd_calls(session, "/unrestrict/link") == []


def test_real_debrid_links_that_arrive_during_settling_stay_individual():
    session = _rd_env([
        _rd_info("waiting_files_selection"),
        _rd_info("downloaded", links=[RD_PACKED_LINK]),
        _rd_info("downloaded"),
    ])
    clock = FakeClock()
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert [f.locked_link for f in cached.files] == [RD_LOCKED_1, RD_LOCKED_2]
    assert _rd_calls(session, "/unrestrict/link") == []


def test_real_debrid_fresh_selection_waits_before_calling_a_link_packed():
    session = _rd_env(
        [_rd_info("waiting_files_selection", files=_rd_selected(6))]
        + [_rd_info(
            "downloaded", files=_rd_selected(6), links=[RD_PACKED_LINK],
        )] * 40,
        **{"/unrestrict/link": _rd_unrestricted()},
    )
    clock = FakeClock()
    started = clock.now
    cached = debrid.real_debrid_cached_torrent(
        INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
    )
    assert cached is not None
    # Polled at the existing interval until the settling deadline, not before.
    assert set(clock.slept) == {debrid.RD_TORRENT_POLL_INTERVAL}
    assert clock.now - started >= debrid.RD_SELECTION_SETTLE_WAIT


def test_real_debrid_packed_failure_survives_a_failing_cleanup():
    session = _rd_env(
        [_rd_info("downloaded", files=_rd_selected(6), links=[RD_PACKED_LINK])],
        **{
            "/unrestrict/link": _rd_unrestricted(filename=""),
            "/torrents/delete/rd-1": RuntimeError("boom"),
        },
    )
    clock = FakeClock()
    with pytest.raises(DebridError) as excinfo:
        debrid.real_debrid_cached_torrent(
            INFO_HASH, TOKEN, session=session, sleep=clock.sleep, clock=clock.monotonic
        )
    assert excinfo.value.code == "packed_unsupported"
    assert len(_rd_calls(session, "/torrents/delete/rd-1")) == 2


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


def test_unlock_torrent_file_rejects_torbox():
    """TorBox is in DEBRID_PROVIDERS (for ordering) but has no locked-link
    credential lookup here -- it must never fall into the Real-Debrid
    credential branch by virtue of not being AllDebrid."""
    with pytest.raises(DebridError) as exc:
        debrid.unlock_torrent_file(
            "https://torbox.app/torrent/1/2", debrid.TORBOX,
            _torrent_settings(), session=FakeSession({}),
        )
    assert exc.value.code == "unknown_provider"


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


# --------------------------------------------------------------------------
# TorBox (T1: hoster/web-download route and provider foundation only)
# --------------------------------------------------------------------------

TORBOX_TOKEN = "torbox-token-SECRET-0123456789"  # gitleaks:allow
TORBOX_LINK = "https://rapidgator.net/file/1"


def _tb_hash(link=TORBOX_LINK):
    import hashlib
    return hashlib.md5(link.encode("utf-8")).hexdigest()


def _tb_env(data):
    return _Resp({"success": True, "error": None, "detail": "", "data": data})


def _tb_entry(files=None, ready=True):
    return {
        "download_present": ready,
        "download_finished": ready,
        "files": [{"id": 555, "name": "file.zip", "size": 100}] if files is None else files,
    }


def _tb_settings(**kwargs):
    base = dict(torbox_enabled=False, torbox_api_token="", debrid_preferred_provider="alldebrid")
    base.update(kwargs)
    return Settings(**base)


@pytest.fixture
def torbox_available(monkeypatch):
    monkeypatch.setattr(debrid, "TORBOX_FEATURE_AVAILABLE", True)


# ---- config: settings, ordering, availability gate ------------------------


def test_torbox_settings_defaults_are_disabled():
    s = Settings()
    assert s.torbox_enabled is False
    assert s.torbox_api_token == ""


def test_torbox_settings_round_trip(settings_env):
    s = Settings()
    s.torbox_enabled = True
    s.torbox_api_token = TORBOX_TOKEN
    s.debrid_preferred_provider = "torbox"
    s.save()
    loaded = Settings.load()
    assert loaded.torbox_enabled is True
    assert loaded.torbox_api_token == TORBOX_TOKEN
    assert loaded.debrid_preferred_provider == "torbox"


def test_torbox_settings_type_guards_reset_bad_values(settings_env):
    Settings().save()
    raw = json.loads(settings_env.read_text())
    raw.update({"torbox_enabled": "yes", "torbox_api_token": 12345})
    settings_env.write_text(json.dumps(raw))
    loaded = Settings.load()
    assert loaded.torbox_enabled is False
    assert loaded.torbox_api_token == ""


def test_torbox_preferred_provider_is_accepted_and_round_trips(settings_env):
    s = Settings()
    s.debrid_preferred_provider = "torbox"
    s.save()
    assert Settings.load().debrid_preferred_provider == "torbox"


def test_invalid_preferred_provider_still_resets_to_alldebrid(settings_env):
    Settings().save()
    raw = json.loads(settings_env.read_text())
    raw["debrid_preferred_provider"] = "premiumize"
    settings_env.write_text(json.dumps(raw))
    assert Settings.load().debrid_preferred_provider == "alldebrid"


def test_torbox_excluded_from_enabled_providers_when_gate_is_off(monkeypatch):
    monkeypatch.setattr(debrid, "TORBOX_FEATURE_AVAILABLE", False)
    settings = _tb_settings(torbox_enabled=True, torbox_api_token=TORBOX_TOKEN)
    assert debrid._enabled_providers(settings) == []
    assert debrid.is_enabled(settings) is False


def test_torbox_included_when_gate_and_setting_are_both_on(torbox_available):
    settings = _tb_settings(torbox_enabled=True, torbox_api_token=TORBOX_TOKEN)
    assert debrid._enabled_providers(settings) == [(debrid.TORBOX, TORBOX_TOKEN)]


def test_torbox_disabled_setting_excludes_it_even_with_gate_on(torbox_available):
    settings = _tb_settings(torbox_enabled=False, torbox_api_token=TORBOX_TOKEN)
    assert debrid._enabled_providers(settings) == []


@pytest.mark.usefixtures("torbox_available")
@pytest.mark.parametrize("preferred,expected", [
    ("alldebrid", [ALL_DEBRID, REAL_DEBRID, debrid.TORBOX]),
    ("real_debrid", [REAL_DEBRID, ALL_DEBRID, debrid.TORBOX]),
    ("torbox", [debrid.TORBOX, ALL_DEBRID, REAL_DEBRID]),
])
def test_three_provider_ordering_is_deterministic(preferred, expected):
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
        debrid_preferred_provider=preferred,
    )
    settings.torbox_enabled = True
    settings.torbox_api_token = TORBOX_TOKEN
    pairs = [p[0] for p in debrid._enabled_providers(settings)]
    assert pairs == expected


def test_two_provider_ordering_is_unchanged_when_torbox_gate_is_off(monkeypatch):
    monkeypatch.setattr(debrid, "TORBOX_FEATURE_AVAILABLE", False)
    settings = _settings(
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
        real_debrid_enabled=True, real_debrid_api_token=TOKEN,
        debrid_preferred_provider="real_debrid",
    )
    settings.torbox_enabled = True
    settings.torbox_api_token = TORBOX_TOKEN
    pairs = [p[0] for p in debrid._enabled_providers(settings)]
    assert pairs == [REAL_DEBRID, ALL_DEBRID]


# ---- hoster domains ---------------------------------------------------


def test_torbox_supported_domains_filters_disabled_hosts():
    session = FakeSession({
        "/webdl/hosters": _tb_env([
            {"name": "RapidGator", "domains": ["rapidgator.net"], "status": True},
            {"name": "Dead Host", "domains": ["deadhost.example"], "status": False},
        ]),
    })
    assert debrid.torbox_supported_domains(session=session) == ["rapidgator.net"]


def test_torbox_domain_matching_is_exact_or_subdomain_not_substring():
    domains = ["rapidgator.net"]
    assert debrid.is_supported_domain("https://rapidgator.net/f", domains) is True
    assert debrid.is_supported_domain("https://cdn.rapidgator.net/f", domains) is True
    assert debrid.is_supported_domain("https://notrapidgator.net.evil.com/f", domains) is False


def test_torbox_is_excluded_as_a_provider_owned_domain():
    assert debrid.is_provider_domain("https://torbox.app/f/abc") is True
    assert debrid.is_provider_domain("https://api.torbox.app/v1/api/webdl/requestdl") is True


# ---- account test -------------------------------------------------------


def test_torbox_account_maps_only_safe_fields():
    session = FakeSession({
        "/user/me": _tb_env({
            "email": "user@example.com",
            "is_subscribed": True,
            "premium_expires_at": "2027-01-01T00:00:00Z",
            "customer": "cus_SECRET123",
            "auth_id": "auth_SECRET456",
            "id": 999,
            "total_downloaded": 123456,
        }),
    })
    account = debrid.torbox_account(TORBOX_TOKEN, session=session)
    assert account == {
        "email": "user@example.com",
        "is_subscribed": True,
        "expiration": "2027-01-01T00:00:00Z",
    }


def test_torbox_account_invalid_token_is_non_fallback():
    session = FakeSession({"/user/me": _Resp({"success": False}, 401)})
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_account(TORBOX_TOKEN, session=session)
    assert excinfo.value.fallback_allowed is False


def test_torbox_account_malformed_response_is_rejected():
    session = FakeSession({"/user/me": _Resp("not-json-object")})
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_account(TORBOX_TOKEN, session=session)
    assert excinfo.value.fallback_allowed is False


def test_torbox_account_error_never_carries_the_token():
    session = FakeSession({"/user/me": _Resp({"success": False, "detail": TORBOX_TOKEN}, 401)})
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_account(TORBOX_TOKEN, session=session)
    text = _all_error_text(excinfo.value)
    assert TORBOX_TOKEN not in text
    assert "SECRET" not in text


# ---- checkcached / create / requestdl -------------------------------------


def test_torbox_check_cached_true_when_hash_present():
    h = _tb_hash()
    session = FakeSession({"/webdl/checkcached": _tb_env({h: True})})
    assert debrid._torbox_check_cached(TORBOX_LINK, TORBOX_TOKEN, session=session) is True


def test_torbox_check_cached_false_when_hash_absent():
    session = FakeSession({"/webdl/checkcached": _tb_env({})})
    assert debrid._torbox_check_cached(TORBOX_LINK, TORBOX_TOKEN, session=session) is False


def test_torbox_check_cached_false_on_ambiguous_response():
    session = FakeSession({"/webdl/checkcached": _tb_env("unexpected-shape")})
    assert debrid._torbox_check_cached(TORBOX_LINK, TORBOX_TOKEN, session=session) is False


def test_torbox_unrestrict_refuses_and_allows_fallback_when_not_cached():
    h = _tb_hash()
    session = FakeSession({"/webdl/checkcached": _tb_env({h: False})})
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_unrestrict(TORBOX_LINK, TORBOX_TOKEN, session=session)
    assert excinfo.value.fallback_allowed is True
    assert excinfo.value.host_unsupported is True


def test_torbox_unrestrict_does_not_call_create_when_not_cached():
    h = _tb_hash()
    session = FakeSession({"/webdl/checkcached": _tb_env({h: False})})
    with pytest.raises(DebridError):
        debrid.torbox_unrestrict(TORBOX_LINK, TORBOX_TOKEN, session=session)
    assert not any("createwebdownload" in url for _, url, _ in session.calls)


def test_torbox_unrestrict_creates_then_unlocks_when_cached():
    h = _tb_hash()
    session = FakeSession({
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 42}),
        "/webdl/mylist": _tb_env(_tb_entry()),
        "/webdl/requestdl": _tb_env("https://cdn-01.torbox.app/dl/secret/file.zip"),
    })
    result = debrid.torbox_unrestrict(TORBOX_LINK, TORBOX_TOKEN, session=session)
    assert result.provider == debrid.TORBOX
    assert result.item_id == "42"
    assert result.filename == "file.zip"
    assert result.filesize == 100
    assert result.download == "https://cdn-01.torbox.app/dl/secret/file.zip"


def test_torbox_unrestrict_zero_filesize_is_accepted():
    h = _tb_hash()
    session = FakeSession({
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 1}),
        "/webdl/mylist": _tb_env(_tb_entry(files=[{"id": 1, "name": "f", "size": 0}])),
        "/webdl/requestdl": _tb_env("https://cdn.torbox.app/f"),
    })
    result = debrid.torbox_unrestrict(TORBOX_LINK, TORBOX_TOKEN, session=session)
    assert result.filesize == 0


def test_torbox_unrestrict_never_polls_past_the_deadline_and_cleans_up():
    h = _tb_hash()
    session = FakeSession({
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 7}),
        "/webdl/mylist": [_tb_env(_tb_entry(ready=False)) for _ in range(2)],
        "/webdl/controlwebdownload": _tb_env(None),
    })
    clock = FakeClock()
    # A sleep that jumps straight past the deadline turns this into a
    # two-iteration test instead of one iteration per second of real wait.
    fast_sleep = lambda _s: clock.sleep(debrid.TORBOX_READY_MAX_WAIT)
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_unrestrict(
            TORBOX_LINK, TORBOX_TOKEN, session=session,
            sleep=fast_sleep, clock=clock.monotonic,
        )
    assert excinfo.value.fallback_allowed is True
    assert any("controlwebdownload" in url for _, url, _ in session.calls)


def test_torbox_create_uses_multipart_form_data():
    h = _tb_hash()
    session = FakeSession({
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 1}),
        "/webdl/mylist": _tb_env(_tb_entry()),
        "/webdl/requestdl": _tb_env("https://cdn.torbox.app/f"),
    })
    debrid.torbox_unrestrict(TORBOX_LINK, TORBOX_TOKEN, session=session)
    method, url, kwargs = next(c for c in session.calls if "createwebdownload" in c[1])
    assert method == "POST"
    assert "files" in kwargs
    assert kwargs["files"]["link"] == (None, TORBOX_LINK)


# ---- requestdl security boundary -------------------------------------------


def test_torbox_requestdl_sends_redirect_false_and_disables_auto_redirect():
    session = FakeSession({"/webdl/requestdl": _tb_env("https://cdn.torbox.app/f")})
    debrid._torbox_request_dl("webdl", "1", "2", TORBOX_TOKEN, session=session)
    method, url, kwargs = session.calls[0]
    assert "redirect=false" in url
    assert kwargs.get("allow_redirects") is False


def test_torbox_requestdl_rejects_its_own_api_host_as_a_delivery_url():
    session = FakeSession({
        "/webdl/requestdl": _tb_env("https://api.torbox.app/v1/api/webdl/requestdl?token=x"),
    })
    with pytest.raises(DebridError) as excinfo:
        debrid._torbox_request_dl("webdl", "1", "2", TORBOX_TOKEN, session=session)
    assert excinfo.value.fallback_allowed is False


def test_torbox_requestdl_rejects_credentials_in_the_delivery_url():
    session = FakeSession({
        "/webdl/requestdl": _tb_env("https://user:pass@cdn.torbox.app/f"),
    })
    with pytest.raises(DebridError):
        debrid._torbox_request_dl("webdl", "1", "2", TORBOX_TOKEN, session=session)


def test_torbox_requestdl_rejects_a_non_http_delivery_url():
    session = FakeSession({"/webdl/requestdl": _tb_env("ftp://cdn.torbox.app/f")})
    with pytest.raises(DebridError):
        debrid._torbox_request_dl("webdl", "1", "2", TORBOX_TOKEN, session=session)


def test_torbox_requestdl_transport_failure_discards_the_original_exception_text():
    class ExplodingSession:
        def get(self, url, **kwargs):
            raise Exception(f"connection failed for {url}?token={TORBOX_TOKEN}")

    with pytest.raises(DebridError) as excinfo:
        debrid._torbox_request_dl("webdl", "1", "2", TORBOX_TOKEN, session=ExplodingSession())
    text = _all_error_text(excinfo.value)
    assert TORBOX_TOKEN not in text
    assert "SECRET" not in text
    assert excinfo.value.__cause__ is None


def test_torbox_requestdl_url_never_reaches_a_raised_error():
    """The tokenized request URL itself must never appear in any raised
    DebridError, even on a clean non-transport failure path."""
    session = FakeSession({"/webdl/requestdl": _Resp({"success": False}, 401)})
    with pytest.raises(DebridError) as excinfo:
        debrid._torbox_request_dl("webdl", "1", "2", TORBOX_TOKEN, session=session)
    text = _all_error_text(excinfo.value)
    assert TORBOX_TOKEN not in text
    assert "requestdl" not in text


# ---- retry / restart: reuse, missing-item recreate -------------------------


def test_torbox_refresh_reuses_the_existing_item_when_ready():
    session = FakeSession({
        "/webdl/mylist": _tb_env(_tb_entry()),
        "/webdl/requestdl": _tb_env("https://cdn.torbox.app/f"),
    })
    settings = _tb_settings(torbox_enabled=True, torbox_api_token=TORBOX_TOKEN)
    result = debrid.torbox_refresh_web_download("42", TORBOX_LINK, settings, session=session)
    assert result.item_id == "42"
    assert not any("createwebdownload" in url for _, url, _ in session.calls)


def test_torbox_refresh_soft_recreates_once_when_item_is_missing():
    h = _tb_hash()
    session = FakeSession({
        # First call (by the stale id "42") reports missing; the second
        # call, made by torbox_unrestrict's own ready-poll against the
        # freshly created id, reports a ready entry.
        "/webdl/mylist": [_tb_env(None), _tb_env(_tb_entry())],
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 99}),
        "/webdl/requestdl": _tb_env("https://cdn.torbox.app/f"),
    })
    settings = _tb_settings(torbox_enabled=True, torbox_api_token=TORBOX_TOKEN)
    result = debrid.torbox_refresh_web_download("42", TORBOX_LINK, settings, session=session)
    assert result.item_id == "99"


def test_torbox_refresh_does_not_recreate_on_auth_failure():
    session = FakeSession({"/webdl/mylist": _Resp({"success": False}, 401)})
    settings = _tb_settings(torbox_enabled=True, torbox_api_token=TORBOX_TOKEN)
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_refresh_web_download("42", TORBOX_LINK, settings, session=session)
    assert excinfo.value.fallback_allowed is False
    assert not any("createwebdownload" in url for _, url, _ in session.calls)


def test_torbox_refresh_does_not_recreate_on_malformed_response():
    session = FakeSession({"/webdl/mylist": _Resp("not-a-dict")})
    settings = _tb_settings(torbox_enabled=True, torbox_api_token=TORBOX_TOKEN)
    with pytest.raises(DebridError):
        debrid.torbox_refresh_web_download("42", TORBOX_LINK, settings, session=session)
    assert not any("createwebdownload" in url for _, url, _ in session.calls)


def test_torbox_refresh_unfinished_item_fails_without_fallback():
    session = FakeSession({"/webdl/mylist": _tb_env(_tb_entry(ready=False))})
    settings = _tb_settings(torbox_enabled=True, torbox_api_token=TORBOX_TOKEN)
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_refresh_web_download("42", TORBOX_LINK, settings, session=session)
    assert excinfo.value.fallback_allowed is False


def test_torbox_refresh_missing_credential_is_reported():
    settings = _tb_settings(torbox_enabled=True, torbox_api_token="")
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_refresh_web_download("42", TORBOX_LINK, settings, session=FakeSession({}))
    assert excinfo.value.provider == debrid.TORBOX


# ---- error mapping (HTTP status only; no guessed error-string casing) -----


def test_torbox_rate_limit_status_allows_fallback():
    session = FakeSession({"/user/me": _Resp({"success": False}, 429)})
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_account(TORBOX_TOKEN, session=session)
    assert excinfo.value.fallback_allowed is True


def test_torbox_server_error_status_allows_fallback():
    session = FakeSession({"/user/me": _Resp({"success": False}, 503)})
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_account(TORBOX_TOKEN, session=session)
    assert excinfo.value.fallback_allowed is True


def test_torbox_unknown_failure_does_not_allow_fallback():
    session = FakeSession({"/user/me": _Resp({"success": False}, 418)})
    with pytest.raises(DebridError) as excinfo:
        debrid.torbox_account(TORBOX_TOKEN, session=session)
    assert excinfo.value.fallback_allowed is False


# ---- resolve() integration --------------------------------------------


def test_resolve_routes_through_torbox_when_preferred(torbox_available):
    h = _tb_hash()
    settings = _tb_settings(
        torbox_enabled=True, torbox_api_token=TORBOX_TOKEN,
        debrid_preferred_provider="torbox",
    )
    session = FakeSession({
        "/webdl/hosters": _tb_env([{"name": "x", "domains": ["rapidgator.net"], "status": True}]),
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 5}),
        "/webdl/mylist": _tb_env(_tb_entry()),
        "/webdl/requestdl": _tb_env("https://cdn.torbox.app/f"),
    })
    result = debrid.resolve(TORBOX_LINK, settings, session=session)
    assert result.provider == debrid.TORBOX
    assert result.item_id == "5"


def test_resolve_falls_back_past_torbox_when_host_unsupported(torbox_available):
    settings = _tb_settings(
        torbox_enabled=True, torbox_api_token=TORBOX_TOKEN,
        debrid_preferred_provider="torbox",
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
    )
    session = FakeSession({
        "/webdl/hosters": _tb_env([{"name": "x", "domains": ["otherhost.example"], "status": True}]),
        "/hosts/domains": _Resp(["rapidgator.net"]),
        "/link/unlock": _ok_ad(),
    })
    result = debrid.resolve("https://rapidgator.net/f", settings, session=session)
    assert result.provider == ALL_DEBRID


def test_resolve_torrent_excludes_torbox_when_gate_is_off(monkeypatch):
    monkeypatch.setattr(debrid, "TORBOX_FEATURE_AVAILABLE", False)
    settings = _tb_settings(torbox_enabled=True, torbox_api_token=TORBOX_TOKEN,
                             debrid_preferred_provider="torbox")
    # Gate is off (the default): TorBox never enters _enabled_providers, so
    # there is nothing to ask and this returns None rather than routing a
    # TorBox credential into a provider probe.
    assert debrid.resolve_torrent(INFO_HASH, settings, session=FakeSession({})) is None


# --------------------------------------------------------------------------
# TorBox (T2: cached torrent routing)
# --------------------------------------------------------------------------

TB_TORRENT_ID = 900


def _tb_torrent_entry(files=None, ready=True, name="Season 1"):
    return {
        "id": TB_TORRENT_ID,
        "name": name,
        "download_present": ready,
        "download_finished": ready,
        "files": (
            [
                {"id": 1, "name": "Season 1/ep1.mkv", "size": 10},
                {"id": 2, "name": "Season 1/extras/ep2.mkv", "size": 20},
            ]
            if files is None else files
        ),
    }


def _tb_checkcached(cached=True):
    return _tb_env([{"hash": INFO_HASH}] if cached else [])


def test_torbox_check_cached_torrent_true_when_present():
    session = FakeSession({"/torrents/checkcached": _tb_checkcached(True)})
    assert debrid._torbox_check_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session) is True


def test_torbox_check_cached_torrent_false_when_absent():
    session = FakeSession({"/torrents/checkcached": _tb_checkcached(False)})
    assert debrid._torbox_check_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session) is False


def test_torbox_cached_torrent_returns_none_when_not_cached():
    session = FakeSession({"/torrents/checkcached": _tb_checkcached(False)})
    assert debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session) is None
    # Not cached means never even attempting a create.
    assert _ad_calls(session, "/torrents/createtorrent") == []


def test_torbox_cached_torrent_creates_then_returns_files():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry()),
    })
    cached = debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)
    assert cached.provider == debrid.TORBOX
    assert cached.info_hash == INFO_HASH
    assert cached.name == "Season 1"
    assert [f.path for f in cached.files] == [("ep1.mkv",), ("extras", "ep2.mkv")]
    assert [f.size for f in cached.files] == [10, 20]
    assert [f.item_id for f in cached.files] == [str(TB_TORRENT_ID), str(TB_TORRENT_ID)]
    assert [f.file_id for f in cached.files] == ["1", "2"]
    assert [f.locked_link for f in cached.files] == ["", ""]


def test_torbox_numeric_id_accepts_whole_valued_floats():
    # TorBox's own schema types these IDs as JSON numbers (e.g. 900.0).
    assert debrid._torbox_numeric_id(900.0) == "900"


def test_torbox_numeric_id_rejects_fractional_and_non_finite_floats():
    for bad in (900.5, float("inf"), float("nan")):
        with pytest.raises(DebridError):
            debrid._torbox_numeric_id(bad)


def test_torbox_numeric_id_rejects_bool():
    with pytest.raises(DebridError):
        debrid._torbox_numeric_id(True)


def test_torbox_cached_torrent_accepts_float_torrent_and_file_ids():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": 900.0}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry(files=[
            {"id": 1.0, "name": "ep1.mkv", "size": 10},
        ])),
    })
    cached = debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)
    assert cached.files[0].item_id == "900"
    assert cached.files[0].file_id == "1"


def test_torbox_create_torrent_sends_multipart_with_cached_only_and_no_seed_no_zip():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry()),
    })
    debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)
    call = next(c for c in session.calls if c[1].endswith("/torrents/createtorrent"))
    files = call[2]["files"]
    assert files["add_only_if_cached"] == (None, "true")
    assert files["seed"] == (None, "3")
    assert files["allow_zip"] == (None, "false")
    assert files["magnet"] == (None, debrid._torrent.minimal_magnet(INFO_HASH))


def test_torbox_create_torrent_uses_torrent_file_bytes_when_given():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry()),
    })
    debrid.torbox_cached_torrent(
        INFO_HASH, TORBOX_TOKEN, torrent_bytes=b"d4:infod...e", session=session,
    )
    call = next(c for c in session.calls if c[1].endswith("/torrents/createtorrent"))
    assert "file" in call[2]["files"]
    assert "magnet" not in call[2]["files"]


def test_torbox_create_torrent_only_ever_receives_the_minimal_magnet():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry()),
    })
    parsed = debrid._torrent.parse_magnet(TRACKER_MAGNET)
    debrid.torbox_cached_torrent(parsed.info_hash, TORBOX_TOKEN, session=session)
    sent = repr(session.calls)
    assert MINIMAL_MAGNET in sent
    for leaked in ("SECRETPASS", "tracker.example", "dn=", "Season+1"):
        assert leaked not in sent


def test_torbox_cached_torrent_add_only_if_cached_refusal_returns_none():
    # checkcached said yes but createtorrent's own cached-only guarantee
    # disagreed and created nothing: no torrent_id means nothing to clean
    # up either.
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({}),
    })
    assert debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session) is None
    assert _ad_calls(session, "/torrents/controltorrent") == []


def test_torbox_cached_torrent_race_deletes_and_returns_none():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry(ready=False)),
        "/torrents/controltorrent": _tb_env(None),
    })
    clock = FakeClock()
    assert debrid.torbox_cached_torrent(
        INFO_HASH, TORBOX_TOKEN, session=session,
        sleep=clock.sleep, clock=clock.monotonic,
    ) is None
    assert len(_ad_calls(session, "/torrents/controltorrent")) == 1


def test_torbox_cached_torrent_deletes_on_malformed_file_list():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry(files=[])),
        "/torrents/controltorrent": _tb_env(None),
    })
    with pytest.raises(DebridError):
        debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)
    assert len(_ad_calls(session, "/torrents/controltorrent")) == 1


def test_torbox_cached_torrent_rejects_duplicate_file_ids():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry(files=[
            {"id": 1, "name": "a.mkv", "size": 1},
            {"id": 1, "name": "b.mkv", "size": 2},
        ])),
        "/torrents/controltorrent": _tb_env(None),
    })
    with pytest.raises(DebridError):
        debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)


def test_torbox_cached_torrent_rejects_unsafe_path():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry(files=[
            {"id": 1, "name": "../../etc/passwd", "size": 1},
        ])),
        "/torrents/controltorrent": _tb_env(None),
    })
    with pytest.raises(DebridError):
        debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)


def test_torbox_cached_torrent_zero_filesize_is_accepted():
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry(files=[
            {"id": 1, "name": "empty.txt", "size": 0},
        ])),
    })
    cached = debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)
    assert cached.files[0].size == 0


def test_torbox_cached_torrent_auth_failure_is_non_fallback():
    session = FakeSession({"/torrents/checkcached": _Resp({}, status_code=401)})
    with pytest.raises(DebridError) as exc:
        debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)
    assert exc.value.fallback_allowed is False


def test_torbox_cached_torrent_rate_limit_allows_fallback():
    session = FakeSession({"/torrents/checkcached": _Resp({}, status_code=429)})
    with pytest.raises(DebridError) as exc:
        debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)
    assert exc.value.fallback_allowed is True


def test_torbox_cached_torrent_transport_failure_never_carries_secrets():
    session = FakeSession({"/torrents/checkcached": RuntimeError(f"boom {TORBOX_TOKEN}")})
    with pytest.raises(DebridError) as exc:
        debrid.torbox_cached_torrent(INFO_HASH, TORBOX_TOKEN, session=session)
    assert TORBOX_TOKEN not in str(exc.value)
    assert TORBOX_TOKEN not in repr(exc.value)


def test_resolve_torrent_routes_through_torbox_when_preferred(torbox_available):
    settings = _tb_settings(
        torbox_enabled=True, torbox_api_token=TORBOX_TOKEN,
        debrid_preferred_provider="torbox",
    )
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(True),
        "/torrents/createtorrent": _tb_env({"torrent_id": TB_TORRENT_ID}),
        "/torrents/mylist": _tb_env(_tb_torrent_entry()),
    })
    cached = debrid.resolve_torrent(INFO_HASH, settings, session=session)
    assert cached.provider == debrid.TORBOX


def test_resolve_torrent_falls_back_past_torbox_when_uncached(torbox_available):
    settings = _tb_settings(
        torbox_enabled=True, torbox_api_token=TORBOX_TOKEN,
        debrid_preferred_provider="torbox",
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
    )
    session = FakeSession({
        "/torrents/checkcached": _tb_checkcached(False),
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _ad_files(),
    })
    cached = debrid.resolve_torrent(INFO_HASH, settings, session=session)
    assert cached.provider == ALL_DEBRID


def test_resolve_torrent_excludes_torbox_when_disabled_even_with_gate_on(torbox_available):
    settings = _tb_settings(
        torbox_enabled=False, torbox_api_token=TORBOX_TOKEN,
        debrid_preferred_provider="torbox",
        all_debrid_enabled=True, all_debrid_api_key=APIKEY,
    )
    session = FakeSession({
        "/magnet/upload": _ad_upload(),
        "/magnet/files": _ad_files(),
    })
    cached = debrid.resolve_torrent(INFO_HASH, settings, session=session)
    assert cached.provider == ALL_DEBRID


def test_torbox_refresh_torrent_file_requests_a_fresh_download():
    session = FakeSession({
        "/torrents/mylist": _tb_env(_tb_torrent_entry()),
        "/torrents/requestdl": _tb_env("https://cdn.torbox.app/f"),
    })
    url = debrid.torbox_refresh_torrent_file(
        str(TB_TORRENT_ID), "1", TORBOX_TOKEN, session=session,
    )
    assert url == "https://cdn.torbox.app/f"
    call = next(c for c in session.calls if "/torrents/requestdl" in c[1])
    assert "torrent_id" in call[1]
    assert "file_id=1" in call[1]
    assert "redirect=false" in call[1]
    assert call[2].get("allow_redirects") is False


def test_torbox_refresh_torrent_file_missing_item_is_a_fixed_error():
    session = FakeSession({"/torrents/mylist": _tb_env(None)})
    with pytest.raises(DebridError) as exc:
        debrid.torbox_refresh_torrent_file(str(TB_TORRENT_ID), "1", TORBOX_TOKEN, session=session)
    assert exc.value.fallback_allowed is False


def test_torbox_refresh_torrent_file_missing_credential_is_reported():
    with pytest.raises(DebridError):
        debrid.torbox_refresh_torrent_file(str(TB_TORRENT_ID), "1", "")


def test_torbox_refresh_torrent_file_not_ready_is_a_fixed_error():
    session = FakeSession({
        "/torrents/mylist": _tb_env(_tb_torrent_entry(ready=False)),
    })
    with pytest.raises(DebridError) as exc:
        debrid.torbox_refresh_torrent_file(str(TB_TORRENT_ID), "1", TORBOX_TOKEN, session=session)
    assert exc.value.code == "not_ready"


def test_torbox_refresh_torrent_file_stale_file_id_is_rejected():
    """A file_id no longer present in the torrent's current file list (the
    torrent was re-checked/re-selected on TorBox's side) must not be sent
    to requestdl as if it were still valid."""
    session = FakeSession({
        "/torrents/mylist": _tb_env(_tb_torrent_entry(files=[
            {"id": 1, "name": "ep1.mkv", "size": 10},
        ])),
    })
    with pytest.raises(DebridError) as exc:
        debrid.torbox_refresh_torrent_file(str(TB_TORRENT_ID), "999", TORBOX_TOKEN, session=session)
    assert exc.value.code == "missing_item"


def test_torbox_refresh_torrent_file_matches_float_file_ids():
    session = FakeSession({
        "/torrents/mylist": _tb_env(_tb_torrent_entry(files=[
            {"id": 1.0, "name": "ep1.mkv", "size": 10},
        ])),
        "/torrents/requestdl": _tb_env("https://cdn.torbox.app/f"),
    })
    url = debrid.torbox_refresh_torrent_file(str(TB_TORRENT_ID), "1", TORBOX_TOKEN, session=session)
    assert url == "https://cdn.torbox.app/f"


def test_torbox_refresh_torrent_file_transport_failure_never_carries_the_token():
    session = FakeSession({
        "/torrents/mylist": _tb_env(_tb_torrent_entry()),
        "/torrents/requestdl": RuntimeError(f"boom {TORBOX_TOKEN}"),
    })
    with pytest.raises(DebridError) as exc:
        debrid.torbox_refresh_torrent_file(str(TB_TORRENT_ID), "1", TORBOX_TOKEN, session=session)
    assert TORBOX_TOKEN not in str(exc.value)
    assert TORBOX_TOKEN not in repr(exc.value)


# ---- TorBox web-download job cleanup ---------------------------------------


def _tb_deletes(session):
    return [c for c in session.calls if "controlwebdownload" in c[1]]


def test_torbox_web_download_is_deleted_when_the_entry_is_malformed():
    """Every unsuccessful exit after the create must release the remote job.

    Only the poll timeout used to clean up, so a malformed response left the
    job on the account - consuming quota with nothing pointing at it.
    """
    h = _tb_hash()
    session = FakeSession({
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 7}),
        "/webdl/mylist": _tb_env(None),          # no entry: bad response
        "/webdl/controlwebdownload": _tb_env(None),
    })

    with pytest.raises(DebridError):
        debrid.torbox_unrestrict(TORBOX_LINK, TORBOX_TOKEN, session=session)

    assert len(_tb_deletes(session)) == 1


def test_torbox_web_download_is_deleted_when_no_file_can_be_picked():
    h = _tb_hash()
    entry = _tb_entry()
    entry["files"] = []                          # nothing selectable
    session = FakeSession({
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 7}),
        "/webdl/mylist": _tb_env(entry),
        "/webdl/controlwebdownload": _tb_env(None),
    })

    with pytest.raises(DebridError):
        debrid.torbox_unrestrict(TORBOX_LINK, TORBOX_TOKEN, session=session)

    assert len(_tb_deletes(session)) == 1


def test_torbox_web_download_is_kept_when_the_unlock_succeeds():
    """The job backs a live download, so success must not clean it up."""
    h = _tb_hash()
    session = FakeSession({
        "/webdl/checkcached": _tb_env({h: True}),
        "/webdl/createwebdownload": _tb_env({"webdownload_id": 7}),
        "/webdl/mylist": _tb_env(_tb_entry()),
        "/webdl/requestdl": _tb_env("https://cdn.torbox.app/f"),
        "/webdl/controlwebdownload": _tb_env(None),
    })

    result = debrid.torbox_unrestrict(TORBOX_LINK, TORBOX_TOKEN, session=session)

    assert result.item_id == "7"
    assert _tb_deletes(session) == []
