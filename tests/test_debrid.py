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
