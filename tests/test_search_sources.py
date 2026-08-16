"""Bounded HTTP plumbing and the built-in source adapters.

Nothing in here touches the network: every request goes through a fake session
that records what it was asked for and replays fixture bytes. A test that
needed a real indexer would be a test that fails whenever the indexer is down.
"""
import json
from pathlib import Path

import pytest
import requests

from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind
from cove.search.sources.base import (
    CONNECT_TIMEOUT,
    MAX_BODY_BYTES,
    MAX_RESULTS,
    READ_TIMEOUT,
    SearchHttp,
    Source,
)


FIXTURES = Path(__file__).parent / "fixtures" / "search"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    """Stands in for requests.Response, with the same surface SearchHttp uses."""

    def __init__(self, body: bytes = b"", status_code: int = 200, chunk_size: int = 4096):
        self.body = body
        self.status_code = status_code
        self.headers = {}
        self.closed = False
        self._chunk_size = chunk_size

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), self._chunk_size):
            yield self.body[start : start + self._chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def close(self):
        self.closed = True


class FakeSession:
    """Records requests and replays queued responses or exceptions."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.cookies = requests.cookies.RequestsCookieJar()
        self.closed = False

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


def json_response(payload) -> FakeResponse:
    return FakeResponse(json.dumps(payload).encode())


# --- SearchHttp ------------------------------------------------------------


def test_get_json_returns_the_decoded_payload():
    http, session = http_with(json_response({"status": "ok"}))
    assert http.get_json("https://example.invalid/api") == {"status": "ok"}
    assert len(session.calls) == 1


def test_get_bytes_returns_the_whole_body():
    http, _ = http_with(FakeResponse(b"<rss></rss>"))
    assert http.get_bytes("https://example.invalid/rss") == b"<rss></rss>"


def test_request_uses_covers_bounded_timeouts_and_keeps_tls_verification():
    http, session = http_with(json_response({}))
    http.get_json("https://example.invalid/api")
    _url, kwargs = session.calls[0]
    assert kwargs["timeout"] == (CONNECT_TIMEOUT, READ_TIMEOUT)
    assert kwargs["stream"] is True
    assert kwargs.get("verify", True) is True


def test_request_sends_an_honest_cove_user_agent():
    from cove import __version__

    http, session = http_with(json_response({}))
    http.get_json("https://example.invalid/api")
    agent = session.calls[0][1]["headers"]["User-Agent"]
    assert agent.startswith("Cove/")
    assert __version__ in agent
    assert "Mozilla" not in agent and "Chrome" not in agent


def test_request_passes_params_through():
    http, session = http_with(json_response({}))
    http.get_json("https://example.invalid/api", params={"q": "dune"})
    assert session.calls[0][1]["params"] == {"q": "dune"}


def test_request_defaults_to_no_params():
    http, session = http_with(json_response({}))
    http.get_json("https://example.invalid/api")
    assert session.calls[0][1]["params"] is None


def test_body_at_the_limit_is_accepted():
    body = b"a" * MAX_BODY_BYTES
    http, _ = http_with(FakeResponse(body))
    assert http.get_bytes("https://example.invalid/big") == body


def test_body_over_the_limit_is_rejected():
    http, session = http_with(FakeResponse(b"a" * (MAX_BODY_BYTES + 1)))
    with pytest.raises(SourceError) as excinfo:
        http.get_bytes("https://example.invalid/huge")
    assert excinfo.value.kind is SourceErrorKind.HTTP
    assert len(session.calls) == 1


def test_oversized_body_is_not_retried_or_fully_buffered():
    response = FakeResponse(b"a" * (MAX_BODY_BYTES * 4), chunk_size=MAX_BODY_BYTES // 2)
    http, session = http_with(response)
    with pytest.raises(SourceError):
        http.get_bytes("https://example.invalid/huge")
    assert response.closed is True
    assert len(session.calls) == 1


def test_connection_error_is_normalised_and_retried_once():
    http, session = http_with(
        requests.ConnectionError("boom"), requests.ConnectionError("boom")
    )
    with pytest.raises(SourceError) as excinfo:
        http.get_json("https://example.invalid/api")
    assert excinfo.value.kind is SourceErrorKind.NETWORK
    assert len(session.calls) == 2


def test_timeout_is_normalised_and_retried_once():
    http, session = http_with(requests.Timeout("slow"), requests.Timeout("slow"))
    with pytest.raises(SourceError) as excinfo:
        http.get_json("https://example.invalid/api")
    assert excinfo.value.kind is SourceErrorKind.TIMEOUT
    assert len(session.calls) == 2


def test_a_transient_failure_is_recovered_by_the_single_retry():
    http, session = http_with(requests.ConnectionError("boom"), json_response({"ok": 1}))
    assert http.get_json("https://example.invalid/api") == {"ok": 1}
    assert len(session.calls) == 2


def test_http_status_failure_is_not_retried():
    http, session = http_with(FakeResponse(b"nope", status_code=503))
    with pytest.raises(SourceError) as excinfo:
        http.get_json("https://example.invalid/api")
    assert excinfo.value.kind is SourceErrorKind.HTTP
    assert len(session.calls) == 1


def test_invalid_json_is_a_parse_error_and_is_not_retried():
    http, session = http_with(FakeResponse(b"<html>nope</html>"))
    with pytest.raises(SourceError) as excinfo:
        http.get_json("https://example.invalid/api")
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


def test_requests_exceptions_never_escape_as_themselves():
    http, _ = http_with(requests.RequestException("odd"), requests.RequestException("odd"))
    with pytest.raises(SourceError):
        http.get_json("https://example.invalid/api")


def test_responses_are_closed():
    response = json_response({})
    http, _ = http_with(response)
    http.get_json("https://example.invalid/api")
    assert response.closed is True


def test_cookies_are_discarded_after_every_request():
    http, session = http_with(json_response({}))
    session.cookies.set("session", "value", domain="example.invalid")
    http.get_json("https://example.invalid/api")
    assert len(session.cookies) == 0


def test_redirects_are_not_followed():
    http, session = http_with(json_response({}))
    http.get_json("https://example.invalid/api")
    assert session.calls[0][1]["allow_redirects"] is False


def test_a_redirect_is_a_failure_not_a_body():
    http, session = http_with(FakeResponse(b"", status_code=302))
    with pytest.raises(SourceError) as excinfo:
        http.get_json("https://example.invalid/api")
    assert excinfo.value.kind is SourceErrorKind.HTTP
    assert len(session.calls) == 1


def test_numeric_coercion_survives_overflowing_values():
    from cove.search.sources.base import coerce_count, coerce_size, coerce_timestamp

    infinity = float("inf")
    assert coerce_size(infinity) is None
    assert coerce_count(infinity) == 0
    assert coerce_timestamp(infinity) is None


def test_an_overflowing_number_only_costs_its_own_row():
    # json.loads turns 1e400 into float("inf"), which int() refuses.
    payload = [
        {
            "name": "Overflowing Row",
            "info_hash": "c9e15763f722f23e98a29decdfae341b98d53056",
            "seeders": 1e400,
            "leechers": "1",
            "size": 1e400,
            "added": 1e400,
            "category": "207",
        },
        {
            "name": "Ordinary Row",
            "info_hash": "0123456789abcdef0123456789abcdef01234567",
            "seeders": "3",
            "leechers": "1",
            "size": "1024",
            "added": "1600000000",
            "category": "207",
        },
    ]
    source = piratebay_source()
    http, _ = http_with(FakeResponse(json.dumps(payload).encode()))
    results = source.search("overflow", Category.MOVIES, http)

    assert [r.name for r in results] == ["Overflowing Row", "Ordinary Row"]
    assert results[0].size_bytes is None
    assert results[0].seeders == 0
    assert results[0].added is None


def test_nyaa_size_parsing_survives_an_overflowing_number():
    from cove.search.sources.nyaa import parse_size

    assert parse_size("1" + "0" * 400 + " GiB") is None


def test_source_contract_is_abstract():
    with pytest.raises(TypeError):
        Source()


# --- YTS -------------------------------------------------------------------

YTS_HOSTS = ("yts.gg", "movies-api.accel.li")

# Hosts that live verification retired: yts.mx no longer resolves, yts.am only
# redirects cross-host, and yts.rs answers the API with a 500.
YTS_DEAD_HOSTS = ("yts.mx", "yts.am", "yts.rs")


def yts_source():
    from cove.search.sources.yts import YtsSource

    return YtsSource()


def test_yts_declares_movies_only():
    source = yts_source()
    assert source.id == "yts"
    assert source.categories == (Category.MOVIES,)
    assert source.reports_swarm is True


def test_yts_normalises_valid_rows():
    source = yts_source()
    http, session = http_with(FakeResponse(fixture("yts_valid.json")))
    results = source.search("harbour", Category.MOVIES, http)

    assert [r.info_hash for r in results] == [
        "c9e15763f722f23e98a29decdfae341b98d53056",
        "0123456789abcdef0123456789abcdef01234567",
        "c9e15763f722f23e98a29decdfae341b98d53055",
    ]
    first = results[0]
    assert isinstance(first, SearchResult)
    assert first.source == "yts"
    assert first.size_bytes == 891752448
    assert first.seeders == 41
    assert first.leechers == 7
    assert first.added == 1567001111
    assert first.magnet.startswith(f"magnet:?xt=urn:btih:{first.info_hash}")
    assert len(session.calls) == 1


def test_yts_distinguishes_quality_variants():
    source = yts_source()
    http, _ = http_with(FakeResponse(fixture("yts_valid.json")))
    names = [r.name for r in source.search("harbour", Category.MOVIES, http)]
    assert names[0] == "Harbour Lights (2019) [720p]"
    assert names[1] == "Harbour Lights (2019) [1080p]"
    assert len(set(names)) == len(names)


def test_yts_sends_the_query_to_the_first_host():
    source = yts_source()
    http, session = http_with(FakeResponse(fixture("yts_valid.json")))
    source.search("harbour lights", Category.MOVIES, http)
    url, kwargs = session.calls[0]
    assert url.startswith("https://yts.gg/api/v2/list_movies.json")
    assert kwargs["params"]["query_term"] == "harbour lights"


def test_yts_search_that_succeeds_costs_one_request_to_the_live_host():
    """A healthy search spends nothing on preflight or on dead mirrors."""
    source = yts_source()
    http, session = http_with(FakeResponse(fixture("yts_valid.json")))
    assert source.search("harbour", Category.MOVIES, http)
    assert len(session.calls) == 1
    assert session.calls[0][0].split("/")[2] == "yts.gg"


def test_yts_never_routes_to_a_retired_host():
    """Dead, redirect-only, and broken mirrors must not come back.

    A host that reliably fails is not redundancy; it just burns the timeout
    budget of every search that reaches it.
    """
    from cove.search.sources import yts as yts_module

    source = yts_source()
    for host in YTS_DEAD_HOSTS:
        assert host not in yts_module.HOSTS
    assert source.homepage.split("/")[2] not in YTS_DEAD_HOSTS

    # Not just the constant: no request may reach a retired host either.
    responses = [FakeResponse(b"nope", status_code=503) for _ in range(8)]
    http, session = http_with(*responses)
    with pytest.raises(SourceError):
        source.search("harbour", Category.MOVIES, http)
    hosts = [url.split("/")[2] for url, _ in session.calls]
    assert hosts
    assert not set(hosts) & set(YTS_DEAD_HOSTS)


def test_yts_drops_malformed_rows_but_keeps_the_good_one():
    source = yts_source()
    http, _ = http_with(FakeResponse(fixture("yts_malformed_rows.json")))
    results = source.search("broken", Category.MOVIES, http)

    assert [r.info_hash for r in results] == [
        "c9e15763f722f23e98a29decdfae341b98d53056",
        "0123456789abcdef0123456789abcdef01234567",
    ]
    assert results[1].name == "Fallback Title [720p]"
    kept = results[0]
    assert kept.seeders == 12
    assert kept.leechers == 0
    assert kept.size_bytes is None
    # The torrent's own date is unusable, so the movie's upload date stands in.
    assert kept.added == 1530000000
    assert results[1].added is None


def test_yts_returns_empty_for_a_legitimate_no_results_payload():
    source = yts_source()
    http, _ = http_with(FakeResponse(fixture("yts_empty.json")))
    assert source.search("nothing", Category.MOVIES, http) == []


def test_yts_raises_parse_for_a_structurally_unusable_payload():
    source = yts_source()
    responses = [FakeResponse(fixture("yts_unusable.json")) for _ in YTS_HOSTS]
    http, session = http_with(*responses)
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.MOVIES, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == len(YTS_HOSTS)


def test_yts_fails_over_to_the_next_host():
    source = yts_source()
    http, session = http_with(
        FakeResponse(b"nope", status_code=503),
        FakeResponse(fixture("yts_valid.json")),
    )
    results = source.search("harbour", Category.MOVIES, http)
    assert results
    assert [url.split("/")[2] for url, _ in session.calls] == list(YTS_HOSTS[:2])


def test_yts_failover_is_bounded_to_the_known_hosts():
    source = yts_source()
    responses = [FakeResponse(b"nope", status_code=503) for _ in YTS_HOSTS]
    http, session = http_with(*responses)
    with pytest.raises(SourceError) as excinfo:
        source.search("harbour", Category.MOVIES, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP
    assert [url.split("/")[2] for url, _ in session.calls] == list(YTS_HOSTS)


def test_yts_caps_the_number_of_results():
    payload = {
        "data": {
            "movies": [
                {
                    "title_long": f"Movie {index}",
                    "torrents": [
                        {
                            "hash": f"{index:040x}",
                            "quality": "1080p",
                            "seeds": 1,
                            "peers": 1,
                        }
                    ],
                }
                for index in range(1, MAX_RESULTS + 51)
            ]
        }
    }
    source = yts_source()
    http, _ = http_with(json_response(payload))
    assert len(source.search("many", Category.MOVIES, http)) == MAX_RESULTS


def test_yts_returns_nothing_for_a_category_it_does_not_serve():
    source = yts_source()
    http, session = http_with(FakeResponse(fixture("yts_valid.json")))
    assert source.search("x", Category.ANIME, http) == []
    assert session.calls == []


# --- Pirate Bay / apibay ---------------------------------------------------


def piratebay_source():
    from cove.search.sources.piratebay import PirateBaySource

    return PirateBaySource()


def test_piratebay_declares_movies_and_tv():
    source = piratebay_source()
    assert source.id == "piratebay"
    assert source.categories == (Category.MOVIES, Category.TV)
    assert source.reports_swarm is True


def test_piratebay_movies_keeps_only_movie_categories():
    source = piratebay_source()
    http, session = http_with(FakeResponse(fixture("piratebay_valid.json")))
    results = source.search("harbour", Category.MOVIES, http)

    assert [r.info_hash for r in results] == [
        "c9e15763f722f23e98a29decdfae341b98d53056",
        "0123456789abcdef0123456789abcdef01234567",
    ]
    first = results[0]
    assert first.source == "piratebay"
    assert first.name == "Harbour Lights 2019 1080p WEB h264"
    assert first.size_bytes == 1879048192
    assert first.seeders == 204
    assert first.leechers == 19
    assert first.added == 1567002222
    assert first.magnet.startswith(f"magnet:?xt=urn:btih:{first.info_hash}")
    # One request per search, whatever the category.
    assert len(session.calls) == 1


def test_piratebay_tv_keeps_only_tv_categories():
    source = piratebay_source()
    http, _ = http_with(FakeResponse(fixture("piratebay_valid.json")))
    results = source.search("long wait", Category.TV, http)
    assert [r.info_hash for r in results] == [
        "c9e15763f722f23e98a29decdfae341b98d53055",
        "0123456789abcdef0123456789abcdef01234568",
    ]


def test_piratebay_all_keeps_every_supported_category():
    source = piratebay_source()
    http, _ = http_with(FakeResponse(fixture("piratebay_valid.json")))
    results = source.search("harbour", Category.ALL, http)
    # The music row is outside both supported category sets and stays out.
    assert len(results) == 4
    assert "0123456789abcdef0123456789abcdef01234569" not in {r.info_hash for r in results}


def test_piratebay_sends_the_query_to_apibay():
    source = piratebay_source()
    http, session = http_with(FakeResponse(fixture("piratebay_valid.json")))
    source.search("harbour lights", Category.MOVIES, http)
    url, kwargs = session.calls[0]
    assert url == "https://apibay.org/q.php"
    assert kwargs["params"]["q"] == "harbour lights"


def test_piratebay_returns_empty_for_the_no_results_placeholder():
    source = piratebay_source()
    http, _ = http_with(FakeResponse(fixture("piratebay_no_results.json")))
    assert source.search("nothing at all", Category.MOVIES, http) == []


def test_piratebay_drops_malformed_rows_but_keeps_the_good_one():
    source = piratebay_source()
    http, _ = http_with(FakeResponse(fixture("piratebay_malformed_rows.json")))
    results = source.search("broken", Category.MOVIES, http)

    assert [r.info_hash for r in results] == ["c9e15763f722f23e98a29decdfae341b98d53056"]
    kept = results[0]
    assert kept.seeders == 0
    assert kept.leechers == 0
    assert kept.size_bytes is None
    assert kept.added is None


def test_piratebay_raises_parse_for_a_structurally_unusable_payload():
    source = piratebay_source()
    http, _ = http_with(FakeResponse(fixture("piratebay_unusable.json")))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.MOVIES, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_piratebay_caps_the_number_of_results():
    payload = [
        {
            "name": f"Row {index}",
            "info_hash": f"{index:040x}",
            "seeders": "1",
            "leechers": "1",
            "size": "1024",
            "added": "1600000000",
            "category": "207",
        }
        for index in range(1, MAX_RESULTS + 51)
    ]
    source = piratebay_source()
    http, _ = http_with(json_response(payload))
    assert len(source.search("many", Category.MOVIES, http)) == MAX_RESULTS


def test_piratebay_returns_nothing_for_a_category_it_does_not_serve():
    source = piratebay_source()
    http, session = http_with(FakeResponse(fixture("piratebay_valid.json")))
    assert source.search("x", Category.ANIME, http) == []
    assert session.calls == []


# --- Nyaa ------------------------------------------------------------------


def nyaa_source():
    from cove.search.sources.nyaa import NyaaSource

    return NyaaSource()


def test_nyaa_declares_anime_only():
    source = nyaa_source()
    assert source.id == "nyaa"
    assert source.categories == (Category.ANIME,)
    assert source.reports_swarm is True


def test_nyaa_normalises_valid_rss_items():
    source = nyaa_source()
    http, session = http_with(FakeResponse(fixture("nyaa_valid.xml")))
    results = source.search("example", Category.ANIME, http)

    assert [r.info_hash for r in results] == [
        "c9e15763f722f23e98a29decdfae341b98d53056",
        "0123456789abcdef0123456789abcdef01234567",
        "c9e15763f722f23e98a29decdfae341b98d53055",
    ]
    first = results[0]
    assert first.source == "nyaa"
    assert first.name == "[Fansub] Example Show - 01 (1080p) [ABCD1234].mkv"
    assert first.seeders == 123
    assert first.leechers == 4
    assert first.size_bytes == 1503238553
    assert first.added == 1691264096
    assert first.magnet.startswith(f"magnet:?xt=urn:btih:{first.info_hash}")
    assert len(session.calls) == 1


def test_nyaa_converts_the_size_units_it_publishes():
    from cove.search.sources.nyaa import parse_size

    assert parse_size("700 Bytes") == 700
    assert parse_size("1.0 KiB") == 1024
    assert parse_size("350.2 MiB") == 367211315
    assert parse_size("1.4 GiB") == 1503238553
    assert parse_size("2 TiB") == 2199023255552
    assert parse_size("1,5 GiB") is None
    assert parse_size("enormous") is None
    assert parse_size("") is None
    assert parse_size(None) is None


def test_nyaa_converts_pubdate_to_a_unix_timestamp():
    from cove.search.sources.nyaa import parse_pubdate

    assert parse_pubdate("Sun, 06 Aug 2023 01:00:00 -0000") == 1691308800
    assert parse_pubdate("not a date at all") is None
    assert parse_pubdate(None) is None


def test_nyaa_queries_the_rss_endpoint():
    source = nyaa_source()
    http, session = http_with(FakeResponse(fixture("nyaa_valid.xml")))
    source.search("example show", Category.ANIME, http)
    url, kwargs = session.calls[0]
    assert url == "https://nyaa.si/"
    assert kwargs["params"]["page"] == "rss"
    assert kwargs["params"]["q"] == "example show"


def test_nyaa_returns_empty_for_a_feed_with_no_items():
    source = nyaa_source()
    http, _ = http_with(FakeResponse(fixture("nyaa_empty.xml")))
    assert source.search("nothing", Category.ANIME, http) == []


def test_nyaa_drops_malformed_items_but_keeps_the_good_one():
    source = nyaa_source()
    http, _ = http_with(FakeResponse(fixture("nyaa_malformed_items.xml")))
    results = source.search("broken", Category.ANIME, http)

    assert [r.info_hash for r in results] == ["c9e15763f722f23e98a29decdfae341b98d53056"]
    kept = results[0]
    assert kept.name == "Usable Despite Bad Metadata"
    assert kept.seeders == 0
    assert kept.leechers == 0
    assert kept.size_bytes is None
    assert kept.added is None


def test_nyaa_raises_parse_for_a_truncated_feed():
    source = nyaa_source()
    http, _ = http_with(FakeResponse(fixture("nyaa_truncated.xml")))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_nyaa_raises_parse_for_a_document_that_is_not_a_feed():
    source = nyaa_source()
    http, _ = http_with(FakeResponse(fixture("nyaa_not_rss.xml")))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_nyaa_caps_the_number_of_results():
    items = "".join(
        "<item><title>Item {i}</title>"
        "<nyaa:infoHash>{h:040x}</nyaa:infoHash>"
        "<nyaa:seeders>1</nyaa:seeders><nyaa:leechers>1</nyaa:leechers>"
        "<nyaa:size>1.0 GiB</nyaa:size></item>".format(i=index, h=index)
        for index in range(1, MAX_RESULTS + 51)
    )
    feed = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">'
        f"<channel>{items}</channel></rss>"
    ).encode()
    source = nyaa_source()
    http, _ = http_with(FakeResponse(feed))
    assert len(source.search("many", Category.ANIME, http)) == MAX_RESULTS


def test_nyaa_returns_nothing_for_a_category_it_does_not_serve():
    source = nyaa_source()
    http, session = http_with(FakeResponse(fixture("nyaa_valid.xml")))
    assert source.search("x", Category.MOVIES, http) == []
    assert session.calls == []


# --- FitGirl ---------------------------------------------------------------

FITGIRL_ENDPOINT = "https://fitgirl-repacks.site/"


def fitgirl_source():
    from cove.search.sources.fitgirl import FitGirlSource

    return FitGirlSource()


def fitgirl_search_page(*hrefs: str) -> bytes:
    """A results page whose entries link to `hrefs`, and nothing else."""
    entries = "".join(
        f'<article id="post-{index}" class="post hentry">'
        f'<h1 class="entry-title"><a href="{href}" rel="bookmark">Entry {index}</a></h1>'
        "</article>"
        for index, href in enumerate(hrefs, start=1)
    )
    return (
        '<!DOCTYPE html><html><body class="search search-results list-view">'
        f'<div id="primary"><main id="main">{entries}</main></div>'
        "</body></html>"
    ).encode()


def fitgirl_page(name: str) -> FakeResponse:
    return FakeResponse(fixture(name))


# Group A - source metadata


def test_fitgirl_declares_games_only():
    source = fitgirl_source()
    assert source.id == "fitgirl"
    assert source.label == "FitGirl"
    assert source.categories == (Category.GAMES,)
    assert source.enabled_default is True
    # FitGirl publishes no swarm counts, so it must say so rather than
    # reporting a fabricated zero as if it had been measured.
    assert source.reports_swarm is False


def test_fitgirl_is_a_source():
    # Whether it is registered is the registry suite's question, not this
    # one's - this file owns the adapter, not Cove's source inventory.
    assert isinstance(fitgirl_source(), Source)


def test_fitgirl_returns_nothing_for_a_category_it_does_not_serve():
    source = fitgirl_source()
    http, session = http_with(fitgirl_page("fitgirl_search_results.html"))
    assert source.search("x", Category.MOVIES, http) == []
    assert session.calls == []


# Group B - search URL and query encoding


def test_fitgirl_queries_the_site_search_endpoint():
    source = fitgirl_source()
    http, session = http_with(fitgirl_page("fitgirl_search_empty.html"))
    source.search("example game", Category.GAMES, http)
    url, kwargs = session.calls[0]
    assert url == FITGIRL_ENDPOINT
    assert kwargs["params"] == {"s": "example game"}


def test_fitgirl_hands_awkward_queries_over_as_a_parameter_not_a_url():
    source = fitgirl_source()
    query = "tom & jerry + 50% ünicode/slash?q"
    http, session = http_with(fitgirl_page("fitgirl_search_empty.html"))
    source.search(query, Category.GAMES, http)
    url, kwargs = session.calls[0]
    # The query is never spliced into the URL, so encoding stays with the
    # transport and the semantic query is passed through untouched.
    assert url == FITGIRL_ENDPOINT
    assert "?" not in url and "&" not in url
    assert kwargs["params"]["s"] == query


def test_fitgirl_serves_the_all_category():
    source = fitgirl_source()
    http, session = http_with(fitgirl_page("fitgirl_search_empty.html"))
    assert source.search("anything", Category.ALL, http) == []
    assert len(session.calls) == 1


# Group C - search-page parsing


def test_fitgirl_visits_the_result_entries_in_page_order():
    source = fitgirl_source()
    http, session = http_with(
        fitgirl_page("fitgirl_search_results.html"),
        fitgirl_page("fitgirl_detail_primary.html"),
        fitgirl_page("fitgirl_detail_secondary.html"),
        fitgirl_page("fitgirl_detail_no_magnet.html"),
    )
    source.search("example", Category.GAMES, http)
    # Navigation, tag, meta and footer links are not entries; the relative
    # second entry resolves against the canonical origin.
    assert [url for url, _ in session.calls] == [
        FITGIRL_ENDPOINT,
        "https://fitgirl-repacks.site/example-game-one/",
        "https://fitgirl-repacks.site/example-game-two/",
        "https://fitgirl-repacks.site/example-game-three/",
    ]


# Group D - canonical-host boundary


def test_fitgirl_only_follows_entries_on_the_canonical_https_host():
    source = fitgirl_source()
    page = fitgirl_search_page(
        "https://fitgirl-repacks.site/ok-absolute/",
        "https://evil.example/elsewhere/",
        "http://fitgirl-repacks.site/downgraded/",
        "//evil.example/protocol-relative/",
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "file:///etc/passwd",
        "https://user:secret@fitgirl-repacks.site/credentials/",
        "https://fitgirl-repacks.site.evil.example/lookalike/",
        "http://[malformed",
        "",
        "/ok-relative/",
    )
    http, session = http_with(
        FakeResponse(page),
        fitgirl_page("fitgirl_detail_no_magnet.html"),
        fitgirl_page("fitgirl_detail_no_magnet.html"),
    )
    source.search("boundary", Category.GAMES, http)
    # Every rejected link is rejected before a request is made, not after.
    assert [url for url, _ in session.calls] == [
        FITGIRL_ENDPOINT,
        "https://fitgirl-repacks.site/ok-absolute/",
        "https://fitgirl-repacks.site/ok-relative/",
    ]


# Group E - magnet extraction


def test_fitgirl_preserves_the_provider_magnet_and_normalises_its_hash():
    source = fitgirl_source()
    http, _ = http_with(
        FakeResponse(fitgirl_search_page("/example-game-one/")),
        fitgirl_page("fitgirl_detail_primary.html"),
    )
    result = source.search("example", Category.GAMES, http)[0]
    assert result.info_hash == "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    # The provider's own magnet is kept verbatim, trackers and all, and its
    # HTML entities are decoded into real separators.
    assert result.magnet == (
        "magnet:?xt=urn:btih:AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555"
        "&dn=Example+Game+One"
        "&tr=udp%3A%2F%2Ftracker.example%3A1337%2Fannounce"
    )
    assert "&amp;" not in result.magnet
    assert "tracker.example" in result.magnet


def test_fitgirl_takes_the_first_valid_magnet_in_the_content_region():
    source = fitgirl_source()
    http, _ = http_with(
        FakeResponse(fitgirl_search_page("/example-game-one/")),
        fitgirl_page("fitgirl_detail_primary.html"),
    )
    results = source.search("example", Category.GAMES, http)
    # Two valid magnets, one result: the first in document order wins.
    assert len(results) == 1
    assert results[0].info_hash == "aaaa1111bbbb2222cccc3333dddd4444eeee5555"


def test_fitgirl_skips_malformed_magnets_and_uses_the_first_usable_one():
    source = fitgirl_source()
    http, _ = http_with(
        FakeResponse(fitgirl_search_page("/example-game-four/")),
        fitgirl_page("fitgirl_detail_malformed_magnet.html"),
    )
    results = source.search("example", Category.GAMES, http)
    assert [r.info_hash for r in results] == ["c9e15763f722f23e98a29decdfae341b98d53055"]


# Group F - complete results


def test_fitgirl_builds_complete_results_from_the_search_and_repack_pages():
    source = fitgirl_source()
    http, session = http_with(
        fitgirl_page("fitgirl_search_results.html"),
        fitgirl_page("fitgirl_detail_primary.html"),
        fitgirl_page("fitgirl_detail_secondary.html"),
        fitgirl_page("fitgirl_detail_no_magnet.html"),
    )
    results = source.search("example", Category.GAMES, http)

    assert [r.name for r in results] == [
        "Example Game One & Friends - v1.0 + 2 DLCs",
        "Example Game Two",
    ]
    assert [r.info_hash for r in results] == [
        "aaaa1111bbbb2222cccc3333dddd4444eeee5555",
        "c9e15763f722f23e98a29decdfae341b98d53056",
    ]
    first = results[0]
    assert isinstance(first, SearchResult)
    assert first.source == "fitgirl"
    # FitGirl publishes no size or swarm data on either page, and inventing
    # either would feed the aggregator numbers nobody measured.
    assert first.size_bytes is None
    assert first.seeders == 0
    assert first.leechers == 0
    # 2026-08-11T20:25:08+00:00 and 2026-07-04T10:00:00+00:00.
    assert first.added == 1786479908
    assert results[1].added == 1783159200
    assert len(session.calls) == 4


def test_fitgirl_leaves_added_unknown_when_the_entry_has_no_timestamp():
    source = fitgirl_source()
    http, _ = http_with(
        FakeResponse(fitgirl_search_page("/example-game-two/")),
        fitgirl_page("fitgirl_detail_secondary.html"),
    )
    assert source.search("example", Category.GAMES, http)[0].added is None


# Group G - empty and broken pages


def test_fitgirl_returns_empty_for_the_explicit_no_results_page():
    source = fitgirl_source()
    http, session = http_with(fitgirl_page("fitgirl_search_empty.html"))
    assert source.search("nothing at all", Category.GAMES, http) == []
    assert len(session.calls) == 1


def test_fitgirl_raises_parse_for_an_unrecognised_search_page():
    source = fitgirl_source()
    http, session = http_with(fitgirl_page("fitgirl_search_unrecognized.html"))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.GAMES, http)
    # A challenge page is a provider failure, not an honest "no results".
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


def test_fitgirl_does_not_turn_a_search_page_failure_into_empty_results():
    source = fitgirl_source()
    http, session = http_with(FakeResponse(b"nope", status_code=503))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.GAMES, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP
    assert len(session.calls) == 1


def test_fitgirl_returns_empty_when_recognised_pages_carry_no_magnet():
    source = fitgirl_source()
    http, _ = http_with(
        fitgirl_page("fitgirl_search_results.html"),
        fitgirl_page("fitgirl_detail_no_magnet.html"),
        fitgirl_page("fitgirl_detail_no_magnet.html"),
        fitgirl_page("fitgirl_detail_no_magnet.html"),
    )
    # Content absence is not transport failure.
    assert source.search("example", Category.GAMES, http) == []


def test_fitgirl_ignores_magnets_outside_the_content_region():
    source = fitgirl_source()
    http, _ = http_with(
        FakeResponse(fitgirl_search_page("/one/", "/two/")),
        fitgirl_page("fitgirl_detail_unrecognized.html"),
        fitgirl_page("fitgirl_detail_secondary.html"),
    )
    results = source.search("example", Category.GAMES, http)
    assert [r.info_hash for r in results] == ["c9e15763f722f23e98a29decdfae341b98d53056"]


# Group H - per-candidate failure isolation


def test_fitgirl_keeps_valid_entries_when_one_repack_page_fails():
    source = fitgirl_source()
    http, session = http_with(
        fitgirl_page("fitgirl_search_results.html"),
        fitgirl_page("fitgirl_detail_primary.html"),
        FakeResponse(b"nope", status_code=503),
        fitgirl_page("fitgirl_detail_secondary.html"),
    )
    results = source.search("example", Category.GAMES, http)
    assert [r.name for r in results] == [
        "Example Game One & Friends - v1.0 + 2 DLCs",
        "Example Game Three",
    ]
    assert len(session.calls) == 4


def test_fitgirl_raises_when_every_repack_page_fails_to_load():
    source = fitgirl_source()
    http, _ = http_with(
        fitgirl_page("fitgirl_search_results.html"),
        FakeResponse(b"nope", status_code=503),
        FakeResponse(b"nope", status_code=503),
        FakeResponse(b"nope", status_code=503),
    )
    with pytest.raises(SourceError) as excinfo:
        source.search("example", Category.GAMES, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP


def test_fitgirl_raises_when_every_repack_page_is_unrecognised():
    source = fitgirl_source()
    http, _ = http_with(
        fitgirl_page("fitgirl_search_results.html"),
        fitgirl_page("fitgirl_detail_unrecognized.html"),
        fitgirl_page("fitgirl_detail_unrecognized.html"),
        fitgirl_page("fitgirl_detail_unrecognized.html"),
    )
    with pytest.raises(SourceError) as excinfo:
        source.search("example", Category.GAMES, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


# Group I - fanout cap


def test_fitgirl_stops_fetching_after_the_repack_page_cap():
    from cove.search.sources.fitgirl import MAX_DETAIL_PAGES

    source = fitgirl_source()
    page = fitgirl_search_page(*[f"/entry-{index}/" for index in range(1, 21)])
    responses = [fitgirl_page("fitgirl_detail_secondary.html")] * MAX_DETAIL_PAGES
    http, session = http_with(FakeResponse(page), *responses)
    results = source.search("many", Category.GAMES, http)

    assert MAX_DETAIL_PAGES == 8
    assert len(session.calls) == 1 + MAX_DETAIL_PAGES
    assert len(results) == MAX_DETAIL_PAGES
    assert not [url for url, _ in session.calls if url.endswith("/entry-9/")]


def test_fitgirl_never_paginates():
    source = fitgirl_source()
    http, session = http_with(fitgirl_page("fitgirl_search_empty.html"))
    source.search("nothing", Category.GAMES, http)
    assert len(session.calls) == 1
    assert session.calls[0][1]["params"] == {"s": "nothing"}


def test_fitgirl_never_bypasses_search_http():
    import inspect

    from cove.search.sources import fitgirl as module

    text = inspect.getsource(module)
    for banned in (
        "import requests",
        "urllib.request",
        "import httpx",
        "import aiohttp",
        "import subprocess",
        "BeautifulSoup",
        "lxml",
    ):
        assert banned not in text


# --- SubsPlease -------------------------------------------------------------

SUBSPLEASE_ENDPOINT = "https://subsplease.org/api/"


def subsplease_source():
    from cove.search.sources.subsplease import SubsPleaseSource

    return SubsPleaseSource()


# Group A - source metadata


def test_subsplease_declares_anime_only():
    source = subsplease_source()
    assert source.id == "subsplease"
    assert source.label == "SubsPlease"
    assert source.categories == (Category.ANIME,)
    assert source.enabled_default is True
    # The search API publishes no swarm counts at all, so the adapter says so
    # rather than reporting a measured-looking zero.
    assert source.reports_swarm is False


def test_subsplease_is_a_source():
    # Whether it is registered is the registry suite's question, not this
    # one's - this file owns the adapter, not Cove's source inventory.
    assert isinstance(subsplease_source(), Source)


def test_subsplease_returns_nothing_for_a_category_it_does_not_serve():
    source = subsplease_source()
    http, session = http_with(FakeResponse(fixture("subsplease_search_results.json")))
    for category in (Category.MOVIES, Category.TV, Category.GAMES):
        assert source.search("x", category, http) == []
    assert session.calls == []


# Group B - search URL and query encoding


def test_subsplease_queries_the_search_api():
    source = subsplease_source()
    http, session = http_with(FakeResponse(fixture("subsplease_search_empty.json")))
    source.search("example anime", Category.ANIME, http)
    url, kwargs = session.calls[0]
    assert url == SUBSPLEASE_ENDPOINT
    # f selects the API's search mode and tz fixes the timezone the release
    # dates are rendered in, so a search does not depend on the local clock.
    assert kwargs["params"] == {"f": "search", "tz": "UTC", "s": "example anime"}


def test_subsplease_hands_awkward_queries_over_as_a_parameter_not_a_url():
    source = subsplease_source()
    query = "tom & jerry + 50% ünicode/slash?q"
    http, session = http_with(FakeResponse(fixture("subsplease_search_empty.json")))
    source.search(query, Category.ANIME, http)
    url, kwargs = session.calls[0]
    # The query is never spliced into the URL, so encoding stays with the
    # transport and the semantic query is passed through untouched.
    assert url == SUBSPLEASE_ENDPOINT
    assert "?" not in url and "&" not in url
    assert kwargs["params"]["s"] == query


def test_subsplease_serves_the_all_category():
    source = subsplease_source()
    http, session = http_with(FakeResponse(fixture("subsplease_search_empty.json")))
    assert source.search("anything", Category.ALL, http) == []
    assert len(session.calls) == 1


# Group C - top-level response classification


def test_subsplease_returns_empty_for_the_explicit_no_results_payload():
    source = subsplease_source()
    http, session = http_with(FakeResponse(fixture("subsplease_search_empty.json")))
    assert source.search("nothing", Category.ANIME, http) == []
    assert len(session.calls) == 1


def test_subsplease_does_not_read_an_empty_object_as_no_results():
    # The API says "nothing matched" with an empty array, never with an empty
    # object, so an empty map is an undocumented answer rather than a search
    # that found nothing.
    source = subsplease_source()
    http, session = http_with(json_response({}))
    with pytest.raises(SourceError) as excinfo:
        source.search("nothing", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


def test_subsplease_raises_parse_for_malformed_json():
    source = subsplease_source()
    http, session = http_with(FakeResponse(b'{"Example Anime - 01": {'))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "not a payload",
        42,
        None,
        True,
        # A populated array is not a shape the API publishes: only the empty
        # array means "no matches", so a non-empty one is a broken contract,
        # not an answer.
        [{"show": "Example Anime"}],
    ],
)
def test_subsplease_raises_parse_for_a_top_level_shape_it_does_not_publish(payload):
    source = subsplease_source()
    http, _ = http_with(json_response(payload))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_subsplease_does_not_read_a_challenge_page_as_no_results():
    source = subsplease_source()
    http, _ = http_with(
        FakeResponse(b"<!DOCTYPE html><html><body>Checking...</body></html>")
    )
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_subsplease_does_not_turn_a_transport_failure_into_empty_results():
    source = subsplease_source()
    http, _ = http_with(FakeResponse(b"nope", status_code=503))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP


def test_subsplease_error_details_do_not_echo_the_query_or_the_body():
    source = subsplease_source()
    http, _ = http_with(json_response({"Example Anime - 01": {"downloads": "gone"}}))
    with pytest.raises(SourceError) as excinfo:
        source.search("a secret query", Category.ANIME, http)
    message = str(excinfo.value)
    assert "secret" not in message
    assert "Example Anime" not in message
    assert len(message) < 200


# Group D - torrent extraction


def test_subsplease_normalises_valid_releases():
    source = subsplease_source()
    http, session = http_with(FakeResponse(fixture("subsplease_search_results.json")))
    results = source.search("example", Category.ANIME, http)

    # Provider order, releases then variants, with no local re-sorting.
    assert [r.info_hash for r in results] == [
        "aaaa1111bbbb2222cccc3333dddd4444eeee5555",
        "1111222233334444555566667777888899990000",
        "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "0123456789abcdef0123456789abcdef01234567",
    ]
    first = results[0]
    assert isinstance(first, SearchResult)
    assert first.source == "subsplease"
    assert first.name == "Example Anime - 01 [480]"
    assert first.added == 1786291423
    # No swarm counts exist in this API, so none are invented - the source's
    # reports_swarm flag is what tells a caller these zeroes are unknowns.
    assert first.seeders == 0 and first.leechers == 0
    assert all(r.size_bytes is None for r in results)
    assert results[-1].added == 1786180500
    assert len(session.calls) == 1


def test_subsplease_preserves_the_provider_magnet_and_normalises_its_hash():
    source = subsplease_source()
    http, _ = http_with(FakeResponse(fixture("subsplease_search_results.json")))
    result = source.search("example", Category.ANIME, http)[0]
    # The API's own magnet is kept verbatim, base32 hash and trackers and all,
    # and only the info_hash beside it is normalised to hex.
    assert result.magnet == (
        "magnet:?xt=urn:btih:VKVBCEN3XMRCFTGMGMZ53XKEITXO4VKV"
        "&dn=%5BSubsPlease%5D%20Example%20Anime%20-%2001%20%28480p%29.mkv"
        "&xl=376124912&tr=http%3A%2F%2Ftracker.example%3A7777%2Fannounce"
    )
    assert result.info_hash == "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    assert "tracker.example" in result.magnet


def test_subsplease_leaves_added_unknown_when_the_release_date_is_unusable():
    source = subsplease_source()
    http, _ = http_with(FakeResponse(fixture("subsplease_search_partial.json")))
    results = source.search("example", Category.ANIME, http)
    assert results[-1].added is None


# Group E - resolution variants


def test_subsplease_keeps_every_resolution_as_its_own_result():
    source = subsplease_source()
    http, _ = http_with(FakeResponse(fixture("subsplease_search_results.json")))
    results = source.search("example", Category.ANIME, http)

    names = [r.name for r in results]
    assert names == [
        "Example Anime - 01 [480]",
        "Example Anime - 01 [720]",
        "Example Anime - 01 [1080]",
        "Example Show - 12 [1080]",
    ]
    # Three different hashes are three different torrents, so no "best"
    # resolution is picked and nothing is collapsed onto one entry.
    assert len(set(r.info_hash for r in results)) == len(results)
    assert len(set(names)) == len(names)


# Group F/G - malformed releases and torrent identities


def test_subsplease_drops_malformed_entries_but_keeps_the_good_ones():
    source = subsplease_source()
    http, _ = http_with(FakeResponse(fixture("subsplease_search_partial.json")))
    results = source.search("example", Category.ANIME, http)

    # A null magnet, an unparseable magnet and a string in place of a download
    # object each cost only themselves; their valid peers survive, including
    # the ones in later releases.
    assert [r.info_hash for r in results] == [
        "aaaa1111bbbb2222cccc3333dddd4444eeee5555",
        "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "aa11bb22cc33dd44ee55ff66aa77bb88cc99dd00",
    ]
    # A release with no resolution still gets a usable name.
    assert results[-1].name == "Example Show - 12"


def test_subsplease_raises_parse_when_no_release_yields_a_torrent():
    source = subsplease_source()
    payload = {
        "Example Anime - 01": {"downloads": [{"res": "1080", "magnet": "broken"}]},
        "Example Show - 12": {"downloads": [{"res": "1080", "magnet": None}]},
    }
    http, _ = http_with(json_response(payload))
    with pytest.raises(SourceError) as excinfo:
        source.search("example", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


# Group J - schema drift must never look like an empty search


def test_subsplease_raises_parse_when_the_downloads_container_is_renamed():
    source = subsplease_source()
    http, session = http_with(
        FakeResponse(fixture("subsplease_search_schema_drift.json"))
    )
    with pytest.raises(SourceError) as excinfo:
        source.search("example", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


# Group H - result cap and request cost


def subsplease_payload(releases: int, per_release: int = 1) -> dict:
    return {
        f"Example Anime - {index:04d}": {
            "release_date": "Sun, 09 Aug 2026 16:03:43 +0000",
            "downloads": [
                {
                    "res": str(variant),
                    "magnet": f"magnet:?xt=urn:btih:{index:036x}{variant:04x}",
                }
                for variant in range(per_release)
            ],
        }
        for index in range(1, releases + 1)
    }


def test_subsplease_caps_the_number_of_results():
    source = subsplease_source()
    payload = subsplease_payload(MAX_RESULTS + 50, per_release=3)
    http, session = http_with(json_response(payload))
    results = source.search("many", Category.ANIME, http)

    assert len(results) == MAX_RESULTS
    # The cap counts flattened torrents, so it lands mid-release rather than
    # on a release boundary.
    assert results[0].info_hash == f"{1:036x}{0:04x}"
    assert results[-1].info_hash == f"{MAX_RESULTS // 3 + 1:036x}{(MAX_RESULTS % 3) - 1:04x}"
    # A larger answer never costs a larger number of requests.
    assert len(session.calls) == 1


def test_subsplease_never_paginates_or_follows_a_result():
    source = subsplease_source()
    http, session = http_with(FakeResponse(fixture("subsplease_search_results.json")))
    source.search("example", Category.ANIME, http)
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == SUBSPLEASE_ENDPOINT
    # No page, offset or cursor parameter, and no second request keyed off a
    # result's own `page` field.
    assert set(kwargs["params"]) == {"f", "tz", "s"}


def test_subsplease_never_bypasses_search_http():
    import inspect

    from cove.search.sources import subsplease as module

    text = inspect.getsource(module)
    for banned in (
        "import requests",
        "urllib.request",
        "import httpx",
        "import aiohttp",
        "import subprocess",
        "selenium",
        "playwright",
        "BeautifulSoup",
        "lxml",
    ):
        assert banned not in text


# --- nekoBT ------------------------------------------------------------------

NEKOBT_ENDPOINT = "https://nekobt.to/api/v1/torrents/search"


def nekobt_source():
    from cove.search.sources.nekobt import NekoBtSource

    return NekoBtSource()


# Group A - identity and category


def test_nekobt_declares_anime_only():
    source = nekobt_source()
    assert source.id == "nekobt"
    assert source.label == "nekoBT"
    assert source.categories == (Category.ANIME,)
    assert source.homepage == "https://nekobt.to"
    assert source.serves(Category.ANIME)
    assert source.serves(Category.ALL)
    assert not source.serves(Category.MOVIES)
    assert not source.serves(Category.TV)
    assert not source.serves(Category.GAMES)


def test_nekobt_is_a_source():
    assert isinstance(nekobt_source(), Source)


def test_nekobt_reports_swarm_because_the_api_publishes_counts():
    assert nekobt_source().reports_swarm is True


def test_nekobt_returns_nothing_for_a_category_it_does_not_serve():
    source = nekobt_source()
    http, session = http_with(FakeResponse(fixture("nekobt_valid.json")))
    assert source.search("example", Category.MOVIES, http) == []
    # A category this source cannot answer costs no request at all.
    assert session.calls == []


# Group B - request shape


def test_nekobt_queries_the_search_api():
    source = nekobt_source()
    http, session = http_with(FakeResponse(fixture("nekobt_empty.json")))
    source.search("example anime", Category.ANIME, http)

    url, kwargs = session.calls[0]
    assert url == NEKOBT_ENDPOINT
    assert kwargs["params"] == {"query": "example anime"}


def test_nekobt_hands_awkward_queries_over_as_a_parameter_not_a_url():
    source = nekobt_source()
    query = "a/b?c=d&e #1"
    http, session = http_with(FakeResponse(fixture("nekobt_empty.json")))
    source.search(query, Category.ANIME, http)

    url, kwargs = session.calls[0]
    assert url == NEKOBT_ENDPOINT
    assert kwargs["params"]["query"] == query


def test_nekobt_serves_the_all_category():
    source = nekobt_source()
    http, session = http_with(FakeResponse(fixture("nekobt_valid.json")))
    assert source.search("example", Category.ALL, http)
    assert len(session.calls) == 1


# Group C - explicit no results


def test_nekobt_returns_empty_for_the_explicit_no_results_payload():
    source = nekobt_source()
    http, session = http_with(FakeResponse(fixture("nekobt_empty.json")))
    assert source.search("nothing", Category.ANIME, http) == []
    assert len(session.calls) == 1


# Group D - malformed responses


def test_nekobt_raises_parse_for_malformed_json():
    source = nekobt_source()
    http, session = http_with(FakeResponse(b'{"error": false, "data": {'))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


def test_nekobt_raises_parse_when_the_results_container_is_renamed():
    # An empty search answers with `results: []`, so a payload with no results
    # container at all is schema drift, not a search that found nothing.
    source = nekobt_source()
    http, session = http_with(FakeResponse(fixture("nekobt_unusable.json")))
    with pytest.raises(SourceError) as excinfo:
        source.search("example", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "not a payload",
        42,
        None,
        True,
        [],
        [{"title": "Example Anime"}],
        {"error": False},
        {"error": False, "data": None},
        {"error": False, "data": []},
        {"error": False, "data": {"results": None}},
        {"error": False, "data": {"results": {}}},
        {"error": False, "data": {"results": "none"}},
    ],
)
def test_nekobt_raises_parse_for_a_shape_the_api_does_not_publish(payload):
    source = nekobt_source()
    http, _ = http_with(json_response(payload))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_nekobt_does_not_read_a_challenge_page_as_no_results():
    source = nekobt_source()
    http, _ = http_with(
        FakeResponse(b"<!DOCTYPE html><html><body>Checking...</body></html>")
    )
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_nekobt_does_not_turn_a_transport_failure_into_empty_results():
    source = nekobt_source()
    http, _ = http_with(FakeResponse(b"nope", status_code=503))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.ANIME, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP


def test_nekobt_error_details_do_not_echo_the_query_or_the_body():
    source = nekobt_source()
    http, _ = http_with(json_response({"error": False, "data": {"items": []}}))
    with pytest.raises(SourceError) as excinfo:
        source.search("a secret query", Category.ANIME, http)
    message = str(excinfo.value)
    assert "secret" not in message
    assert "items" not in message
    assert len(message) < 200


# Group E - normalisation, swarm, size and date


def test_nekobt_normalises_valid_results():
    source = nekobt_source()
    http, session = http_with(FakeResponse(fixture("nekobt_valid.json")))
    results = source.search("example", Category.ANIME, http)

    assert len(session.calls) == 1
    assert [r.name for r in results] == [
        "[Example] Example Anime 01-12 (BD 1080p)",
        "[Example] Example Show - 12 (WEB 720p)",
        "[Example] Example Movie (BD 2160p)",
    ]
    assert [r.source for r in results] == ["nekobt", "nekobt", "nekobt"]
    assert [r.info_hash for r in results] == [
        "aaaa1111bbbb2222cccc3333dddd4444eeee5555",
        "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "aa11bb22cc33dd44ee55ff66aa77bb88cc99dd00",
    ]


def test_nekobt_keeps_the_provider_magnet_verbatim():
    source = nekobt_source()
    payload = json.loads(fixture("nekobt_valid.json"))
    http, _ = http_with(FakeResponse(fixture("nekobt_valid.json")))
    results = source.search("example", Category.ANIME, http)

    # The API hands out a complete magnet, so it is passed through untouched
    # and only its hash is normalised for the result identity.
    assert [r.magnet for r in results] == [
        row["magnet"] for row in payload["data"]["results"]
    ]


def test_nekobt_keeps_the_swarm_counts_the_api_reports():
    source = nekobt_source()
    http, _ = http_with(FakeResponse(fixture("nekobt_valid.json")))
    results = source.search("example", Category.ANIME, http)

    # The API sends these as strings; a reported zero stays a zero.
    assert [(r.seeders, r.leechers) for r in results] == [(42, 7), (0, 0), (5, 1)]


def test_nekobt_reads_filesize_as_a_byte_count():
    source = nekobt_source()
    http, _ = http_with(FakeResponse(fixture("nekobt_valid.json")))
    results = source.search("example", Category.ANIME, http)

    # `filesize` is a decimal string of bytes, and null means "not reported".
    assert [r.size_bytes for r in results] == [16657087194, 1073741824, None]


def test_nekobt_converts_uploaded_at_from_milliseconds():
    source = nekobt_source()
    http, _ = http_with(FakeResponse(fixture("nekobt_valid.json")))
    results = source.search("example", Category.ANIME, http)

    # `uploaded_at` is epoch milliseconds; SearchResult.added is seconds.
    assert [r.added for r in results] == [1785499942, 1764126575, None]


# Group F - torrent identity


def test_nekobt_derives_the_hash_from_the_magnet_it_was_given():
    source = nekobt_source()
    payload = {
        "error": False,
        "data": {
            "results": [
                {
                    "title": "[Example] Disagreeing Fields",
                    # A provider `infohash` that contradicts the magnet must
                    # never become the result identity: the magnet Cove hands
                    # to the torrent path is the one that decides.
                    "infohash": "1111111111111111111111111111111111111111",
                    "magnet": (
                        "magnet:?xt=urn:btih:"
                        "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
                    ),
                    "filesize": "1024",
                    "seeders": "1",
                    "leechers": "0",
                    "uploaded_at": 1785499942639,
                }
            ]
        },
    }
    http, _ = http_with(json_response(payload))
    (result,) = source.search("example", Category.ANIME, http)

    assert result.info_hash == "aaaa1111bbbb2222cccc3333dddd4444eeee5555"
    from cove.search.magnet import extract_info_hash

    assert extract_info_hash(result.magnet) == result.info_hash


def test_nekobt_drops_malformed_rows_but_keeps_the_good_ones():
    source = nekobt_source()
    http, _ = http_with(FakeResponse(fixture("nekobt_malformed_rows.json")))
    results = source.search("example", Category.ANIME, http)

    # A string in place of a row, a null magnet, an unparseable magnet, a
    # blank title and the all-zero placeholder hash each cost only themselves.
    assert [r.info_hash for r in results] == [
        "cccc1111dddd2222eeee3333ffff44445555aaaa",
        "dddd1111eeee2222ffff33334444555566667777",
    ]
    # Junk in the optional fields does not take a usable torrent down with it,
    # and it is never replaced with an invented value.
    junk = results[0]
    assert junk.size_bytes is None
    assert junk.added is None
    assert (junk.seeders, junk.leechers) == (0, 0)


# Group G - result cap and request cost


def nekobt_payload(rows: int) -> dict:
    return {
        "error": False,
        "data": {
            "results": [
                {
                    "title": f"[Example] Example Anime - {index:04d}",
                    "infohash": f"{index:040x}",
                    "magnet": f"magnet:?xt=urn:btih:{index:040x}",
                    "filesize": "1024",
                    "seeders": "1",
                    "leechers": "0",
                    "uploaded_at": 1785499942639,
                }
                for index in range(1, rows + 1)
            ],
            "more": True,
        },
    }


def test_nekobt_caps_the_number_of_results():
    source = nekobt_source()
    http, session = http_with(json_response(nekobt_payload(MAX_RESULTS + 50)))
    results = source.search("many", Category.ANIME, http)

    assert len(results) == MAX_RESULTS
    assert results[0].info_hash == f"{1:040x}"
    assert results[-1].info_hash == f"{MAX_RESULTS:040x}"
    # A larger answer never costs a larger number of requests.
    assert len(session.calls) == 1


def test_nekobt_never_paginates_or_follows_a_result():
    source = nekobt_source()
    # `more` is true and every row carries an id, yet neither buys a request.
    http, session = http_with(json_response(nekobt_payload(3)))
    source.search("example", Category.ANIME, http)

    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == NEKOBT_ENDPOINT
    assert set(kwargs["params"]) == {"query"}


def test_nekobt_never_bypasses_search_http():
    import inspect

    from cove.search.sources import nekobt as module

    text = inspect.getsource(module)
    for banned in (
        "import requests",
        "urllib.request",
        "import httpx",
        "import aiohttp",
        "import subprocess",
        "selenium",
        "playwright",
        "BeautifulSoup",
        "lxml",
    ):
        assert banned not in text


# Group H - registration. The adapter is now a shipped source; which position
# and category it holds is pinned in tests/test_search_registry.py, where the
# whole registry is stated in one place rather than per adapter.


# --- GOG Games ---------------------------------------------------------------

GOGGAMES_ENDPOINT = "https://gog-games.to/search"


def goggames_source():
    from cove.search.sources.goggames import GogGamesSource

    return GogGamesSource()


# Group A - identity and category


def test_goggames_declares_games_only():
    source = goggames_source()
    assert source.id == "goggames"
    assert source.label == "GOG Games"
    assert source.categories == (Category.GAMES,)
    assert source.homepage == "https://gog-games.to"
    assert source.serves(Category.GAMES)
    assert source.serves(Category.ALL)
    assert not source.serves(Category.MOVIES)
    assert not source.serves(Category.TV)
    assert not source.serves(Category.ANIME)


def test_goggames_is_a_source():
    assert isinstance(goggames_source(), Source)


def test_goggames_does_not_report_swarm_because_the_api_publishes_no_counts():
    assert goggames_source().reports_swarm is False


def test_goggames_returns_nothing_for_a_category_it_does_not_serve():
    source = goggames_source()
    http, session = http_with(FakeResponse(fixture("goggames_valid.json")))
    assert source.search("example", Category.MOVIES, http) == []
    # A category this source cannot answer costs no request at all.
    assert session.calls == []


# Group B - request shape
#
# The live endpoint filters on `search`. `query` - the parameter the planning
# sketch recorded - is accepted and silently ignored, and the paginator echoes
# it back in `links`/`meta`, which is what makes the mistake look right. Sending
# `query` returns the whole catalogue in reverse-alphabetical order rather than
# the user's matches, so this is a correctness test, not a style one.


def test_goggames_filters_on_the_search_parameter_not_query():
    source = goggames_source()
    http, session = http_with(FakeResponse(fixture("goggames_empty.json")))
    source.search("example game", Category.GAMES, http)

    url, kwargs = session.calls[0]
    assert url == GOGGAMES_ENDPOINT
    assert kwargs["params"] == {"search": "example game", "page": 1}
    # `query` is the ignored parameter: sending it would return the unfiltered
    # catalogue while still looking like a successful search.
    assert "query" not in kwargs["params"]


def test_goggames_hands_awkward_queries_over_as_a_parameter_not_a_url():
    source = goggames_source()
    query = "a/b?c=d&e #1"
    http, session = http_with(FakeResponse(fixture("goggames_empty.json")))
    source.search(query, Category.GAMES, http)

    url, kwargs = session.calls[0]
    assert url == GOGGAMES_ENDPOINT
    assert kwargs["params"]["search"] == query


def test_goggames_serves_the_all_category():
    source = goggames_source()
    http, session = http_with(FakeResponse(fixture("goggames_valid.json")))
    assert source.search("example", Category.ALL, http)
    assert len(session.calls) == 1


# Group C - explicit no results


def test_goggames_returns_empty_for_the_explicit_no_results_payload():
    source = goggames_source()
    http, session = http_with(FakeResponse(fixture("goggames_empty.json")))
    assert source.search("nothing", Category.GAMES, http) == []
    assert len(session.calls) == 1


# Group D - malformed responses


def test_goggames_raises_parse_for_malformed_json():
    source = goggames_source()
    http, session = http_with(FakeResponse(b'{"data": [{'))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.GAMES, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


def test_goggames_raises_parse_when_the_results_container_is_renamed():
    # A search that matched nothing still answers with `data: []`, so a payload
    # with no `data` list at all is schema drift, not an empty search.
    source = goggames_source()
    http, session = http_with(FakeResponse(fixture("goggames_unusable.json")))
    with pytest.raises(SourceError) as excinfo:
        source.search("example", Category.GAMES, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "not a payload",
        42,
        None,
        True,
        [],
        [{"title": "Example Game"}],
        {},
        {"data": None},
        {"data": {}},
        {"data": "none"},
        {"meta": {"total": 0}},
    ],
)
def test_goggames_raises_parse_for_a_shape_the_api_does_not_publish(payload):
    source = goggames_source()
    http, _ = http_with(json_response(payload))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.GAMES, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_goggames_does_not_read_a_challenge_page_as_no_results():
    source = goggames_source()
    http, _ = http_with(
        FakeResponse(b"<!DOCTYPE html><html><body>Checking...</body></html>")
    )
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.GAMES, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_goggames_does_not_turn_a_transport_failure_into_empty_results():
    source = goggames_source()
    http, _ = http_with(FakeResponse(b"nope", status_code=503))
    with pytest.raises(SourceError) as excinfo:
        source.search("x", Category.GAMES, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP


def test_goggames_error_details_do_not_echo_the_query_or_the_body():
    source = goggames_source()
    http, _ = http_with(json_response({"items": []}))
    with pytest.raises(SourceError) as excinfo:
        source.search("a secret query", Category.GAMES, http)
    message = str(excinfo.value)
    assert "secret" not in message
    assert "items" not in message
    assert len(message) < 200


# Group E - normalisation, swarm, size and date


def test_goggames_normalises_valid_results():
    source = goggames_source()
    http, session = http_with(FakeResponse(fixture("goggames_valid.json")))
    results = source.search("example", Category.GAMES, http)

    assert len(session.calls) == 1
    # The fourth fixture row has a null infohash - a catalogue entry with no
    # release behind it yet - so it never becomes a result.
    assert [r.name for r in results] == [
        "Example Quest - Definitive Edition",
        "Sons & Daughters + Extra Content",
        "黎明为歌 - Example Melody",
    ]
    assert [r.source for r in results] == ["goggames", "goggames", "goggames"]
    # The second row's hash arrives upper-case and is normalised down.
    assert [r.info_hash for r in results] == [
        "aaaa1111bbbb2222cccc3333dddd4444eeee5555",
        "bbbb1111cccc2222dddd3333eeee4444ffff5555",
        "cccc1111dddd2222eeee3333ffff44445555aaaa",
    ]


def test_goggames_reports_no_swarm_counts_rather_than_inventing_them():
    source = goggames_source()
    http, _ = http_with(FakeResponse(fixture("goggames_valid.json")))
    results = source.search("example", Category.GAMES, http)

    # The API publishes no seeder or leecher counts at all. SearchResult needs
    # integers, so they are 0 - and `reports_swarm=False` is what tells callers
    # that the 0 means "not reported" rather than "nobody is seeding".
    assert [(r.seeders, r.leechers) for r in results] == [(0, 0), (0, 0), (0, 0)]
    assert source.reports_swarm is False


def test_goggames_leaves_size_unknown_rather_than_calling_it_zero():
    source = goggames_source()
    http, session = http_with(FakeResponse(fixture("goggames_valid.json")))
    results = source.search("example", Category.GAMES, http)

    # No row carries a byte count and no detail page is fetched to find one.
    assert [r.size_bytes for r in results] == [None, None, None]
    assert len(session.calls) == 1


def test_goggames_dates_a_result_by_last_update():
    source = goggames_source()
    http, _ = http_with(FakeResponse(fixture("goggames_valid.json")))
    results = source.search("example", Category.GAMES, http)

    # `last_update` is when the release itself last changed, which is what
    # SearchResult.added means here. `release_timestamp` is the game's original
    # store date - 2014 for the first row - and dating a 2025 repack by it would
    # be wrong, so it is never read. A null `last_update` stays unknown.
    assert [r.added for r in results] == [1754544686, 1779671651, None]


def test_goggames_ignores_release_timestamp_even_when_last_update_is_missing():
    source = goggames_source()
    payload = {
        "data": [
            {
                "slug": "example",
                "title": "Example Game",
                "release_timestamp": 1417039200,
                "last_update": None,
                "infohash": "aaaa1111bbbb2222cccc3333dddd4444eeee5555",
            }
        ]
    }
    http, _ = http_with(json_response(payload))
    (result,) = source.search("example", Category.GAMES, http)
    assert result.added is None


# Group F - torrent identity and the magnet Cove builds


def test_goggames_builds_the_magnet_from_the_validated_hash_and_title():
    from cove.search.magnet import build_magnet

    source = goggames_source()
    http, _ = http_with(FakeResponse(fixture("goggames_valid.json")))
    results = source.search("example", Category.GAMES, http)

    # The API hands out a bare info hash and no magnet, so Cove synthesises one
    # exactly the way it does for every other hash-only source.
    assert [r.magnet for r in results] == [
        build_magnet(r.info_hash, r.name) for r in results
    ]


def test_goggames_magnet_round_trips_to_the_result_hash():
    from cove.search.magnet import extract_info_hash

    source = goggames_source()
    http, _ = http_with(FakeResponse(fixture("goggames_valid.json")))
    results = source.search("example", Category.GAMES, http)

    assert results
    for result in results:
        assert extract_info_hash(result.magnet) == result.info_hash


def test_goggames_encodes_the_display_name_without_changing_the_title():
    from urllib.parse import parse_qs, urlparse

    source = goggames_source()
    http, _ = http_with(FakeResponse(fixture("goggames_valid.json")))
    results = source.search("example", Category.GAMES, http)

    awkward = results[1]
    # The title needs encoding to survive a magnet query string, but the name
    # the user sees is the provider's, unencoded.
    assert awkward.name == "Sons & Daughters + Extra Content"
    fields = parse_qs(urlparse(awkward.magnet).query)
    assert fields["dn"] == ["Sons & Daughters + Extra Content"]
    assert "Sons+%26+Daughters" in awkward.magnet


def test_goggames_uses_coves_own_tracker_list_and_adds_nothing_else():
    from urllib.parse import parse_qs, urlparse

    from cove.search.magnet import TRACKERS

    source = goggames_source()
    http, _ = http_with(FakeResponse(fixture("goggames_valid.json")))
    results = source.search("example", Category.GAMES, http)

    for result in results:
        fields = parse_qs(urlparse(result.magnet).query)
        # Cove's fixed list, and only it: no announce URL is carried over from
        # anywhere else, and no field beyond the three Cove builds appears.
        assert fields["tr"] == list(TRACKERS)
        assert set(fields) == {"xt", "dn", "tr"}


def test_goggames_drops_rows_it_cannot_give_a_torrent_identity():
    source = goggames_source()
    http, _ = http_with(FakeResponse(fixture("goggames_malformed_rows.json")))
    results = source.search("example", Category.GAMES, http)

    # A string in place of a row, a missing hash, a non-hex hash, a short hash,
    # the all-zero placeholder and a blank title each cost only themselves.
    assert [r.name for r in results] == [
        "Example Unparseable Date",
        "Example Usable Repack",
    ]
    assert [r.info_hash for r in results] == [
        "cccc1111dddd2222eeee3333ffff44445555aaaa",
        "dddd1111eeee2222ffff33334444555566667777",
    ]
    # An unparseable date is not worth discarding a usable torrent over, and it
    # is never replaced with an invented one.
    assert results[0].added is None
    assert results[1].added == 1684431016


# Group G - result cap and request cost


def goggames_payload(rows: int) -> dict:
    return {
        "data": [
            {
                "id": str(index),
                "slug": f"example_game_{index:04d}",
                "title": f"Example Game {index:04d}",
                "release_timestamp": 1417039200,
                "last_update": "2025-08-07T05:31:26.000000Z",
                "infohash": f"{index:040x}",
            }
            for index in range(1, rows + 1)
        ],
        "links": {"next": "https://gog-games.to/search?query=&page=2"},
        "meta": {"current_page": 1, "last_page": 178, "per_page": 36, "total": rows},
    }


def test_goggames_caps_the_number_of_results():
    source = goggames_source()
    http, session = http_with(json_response(goggames_payload(MAX_RESULTS + 50)))
    results = source.search("many", Category.GAMES, http)

    assert len(results) == MAX_RESULTS
    assert results[0].info_hash == f"{1:040x}"
    assert results[-1].info_hash == f"{MAX_RESULTS:040x}"
    # A larger answer never costs a larger number of requests.
    assert len(session.calls) == 1


def test_goggames_never_paginates_or_follows_a_slug():
    source = goggames_source()
    # `meta.last_page` is 178, `links.next` is populated and every row carries a
    # slug that resolves to a detail page. None of that buys a second request.
    http, session = http_with(json_response(goggames_payload(3)))
    source.search("example", Category.GAMES, http)

    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == GOGGAMES_ENDPOINT
    assert set(kwargs["params"]) == {"search", "page"}
    assert kwargs["params"]["page"] == 1


def test_goggames_never_bypasses_search_http():
    import inspect

    from cove.search.sources import goggames as module

    text = inspect.getsource(module)
    for banned in (
        "import requests",
        "urllib.request",
        "import httpx",
        "import aiohttp",
        "import subprocess",
        "selenium",
        "playwright",
        "BeautifulSoup",
        "lxml",
    ):
        assert banned not in text


# Group H - registration. See tests/test_search_registry.py.


# --- Torrents-CSV -----------------------------------------------------------
#
# The index publishes no magnet and no category: Cove builds the magnet from
# the hash it validated, and one query answers both Movies and TV.


def torrentscsv_source():
    from cove.search.sources.torrentscsv import TorrentsCsvSource

    return TorrentsCsvSource()


def torrentscsv_payload(rows: int) -> bytes:
    """A response carrying `rows` rows, seeders ascending.

    Ascending on purpose: the adapter imposes its own descending order, so a
    fixture already in that order could not tell sorting from coincidence.
    """
    return json.dumps(
        {
            "torrents": [
                {
                    "infohash": f"{index:040x}",
                    "name": f"Invented Row {index}",
                    "size_bytes": 1024,
                    "created_unix": 1649403463,
                    "seeders": index,
                    "leechers": 0,
                }
                for index in range(1, rows + 1)
            ],
            "next": None,
        }
    ).encode()


def test_torrentscsv_serves_movies_and_tv_only():
    source = torrentscsv_source()

    assert source.serves(Category.MOVIES)
    assert source.serves(Category.TV)
    assert not source.serves(Category.GAMES)
    assert not source.serves(Category.ANIME)


def test_torrentscsv_returns_nothing_for_a_category_it_does_not_serve():
    source = torrentscsv_source()
    http, session = http_with(FakeResponse(fixture("torrentscsv_valid.json")))

    assert source.search("example", Category.GAMES, http) == []
    assert source.search("example", Category.ANIME, http) == []
    # A category it does not serve costs the index nothing at all.
    assert session.calls == []


def test_torrentscsv_asks_once_with_the_query_as_a_parameter():
    from cove.search.sources.torrentscsv import ENDPOINT, PAGE_SIZE

    source = torrentscsv_source()
    http, session = http_with(FakeResponse(fixture("torrentscsv_valid.json")))
    source.search("breaking bad", Category.TV, http)

    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == ENDPOINT
    assert kwargs["params"] == {"q": "breaking bad", "size": PAGE_SIZE}


def test_torrentscsv_answers_movies_and_tv_with_the_same_one_request():
    source = torrentscsv_source()
    http, session = http_with(
        FakeResponse(fixture("torrentscsv_valid.json")),
        FakeResponse(fixture("torrentscsv_valid.json")),
    )
    movies = source.search("example", Category.MOVIES, http)
    tv = source.search("example", Category.TV, http)

    # The index has no category field, so neither category is a special case.
    assert [r.info_hash for r in movies] == [r.info_hash for r in tv]
    assert session.calls[0][1]["params"] == session.calls[1][1]["params"]


def test_torrentscsv_serves_the_all_category():
    source = torrentscsv_source()
    http, session = http_with(FakeResponse(fixture("torrentscsv_valid.json")))

    assert len(source.search("example", Category.ALL, http)) == 3
    assert len(session.calls) == 1


def test_torrentscsv_orders_by_seeders_rather_than_trusting_the_index():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(fixture("torrentscsv_valid.json")))
    results = source.search("example", Category.MOVIES, http)

    assert [r.seeders for r in results] == [879, 588, 12]


def test_torrentscsv_builds_a_magnet_carrying_the_validated_hash():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(fixture("torrentscsv_valid.json")))
    results = source.search("example", Category.MOVIES, http)

    best = results[0]
    assert best.info_hash == "34ff1fae9661d72152fb1fc31e27c15297072654"
    assert best.magnet.startswith(f"magnet:?xt=urn:btih:{best.info_hash}")
    assert "dn=" in best.magnet


def test_torrentscsv_normalises_an_upper_case_hash():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(fixture("torrentscsv_valid.json")))
    results = source.search("example", Category.MOVIES, http)

    hashes = [r.info_hash for r in results]
    assert "959ed8be27cffcb61603eff33d393c94df2be2d7" in hashes
    for value in hashes:
        assert value == value.lower()


def test_torrentscsv_reads_the_creation_date_not_the_scrape_date():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(fixture("torrentscsv_valid.json")))
    results = source.search("example", Category.MOVIES, http)

    # scraped_date moves on every crawl and would date every row to roughly
    # now; created_unix is when the torrent itself was made.
    assert results[0].added == 1649403463
    assert all(r.added != 1783758257 for r in results)


def test_torrentscsv_keeps_the_sizes_the_index_publishes():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(fixture("torrentscsv_valid.json")))
    results = source.search("example", Category.MOVIES, http)

    assert results[0].size_bytes == 10575330041


def test_torrentscsv_returns_empty_for_a_search_that_matched_nothing():
    source = torrentscsv_source()
    http, session = http_with(FakeResponse(fixture("torrentscsv_empty.json")))

    # An honest "no results" is not a failure and costs no second request.
    assert source.search("nothing", Category.MOVIES, http) == []
    assert len(session.calls) == 1


def test_torrentscsv_raises_parse_for_a_structurally_unusable_payload():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(fixture("torrentscsv_unusable.json")))

    # `torrents` renamed is schema drift, not an empty search - reporting it as
    # "no matches" would hide the break behind a normal-looking result.
    with pytest.raises(SourceError) as error:
        source.search("example", Category.MOVIES, http)
    assert error.value.kind is SourceErrorKind.PARSE


def test_torrentscsv_raises_parse_when_the_payload_is_not_an_object():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(b"[]"))

    with pytest.raises(SourceError) as error:
        source.search("example", Category.MOVIES, http)
    assert error.value.kind is SourceErrorKind.PARSE


def test_torrentscsv_drops_malformed_rows_but_keeps_the_good_ones():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(fixture("torrentscsv_malformed_rows.json")))
    results = source.search("example", Category.MOVIES, http)

    names = [r.name for r in results]
    assert names == ["Invented Good Row 1080p", "Invented Row With Unusable Numbers"]
    # An unusable swarm count, size or date costs the row only those fields.
    bad = results[1]
    assert (bad.seeders, bad.leechers) == (0, 0)
    assert bad.size_bytes is None
    assert bad.added is None


def test_torrentscsv_caps_the_number_of_results():
    source = torrentscsv_source()
    http, session = http_with(FakeResponse(torrentscsv_payload(MAX_RESULTS + 50)))
    results = source.search("many", Category.MOVIES, http)

    assert len(results) == MAX_RESULTS
    # A larger payload never costs a larger number of requests.
    assert len(session.calls) == 1


def test_torrentscsv_never_paginates():
    source = torrentscsv_source()
    # The envelope carries a `next` cursor; reading it would multiply what a
    # search costs the index for results the cap would mostly discard.
    payload = json.loads(fixture("torrentscsv_valid.json"))
    payload["next"] = 25
    http, session = http_with(FakeResponse(json.dumps(payload).encode()))
    source.search("example", Category.MOVIES, http)

    assert len(session.calls) == 1


def test_torrentscsv_stays_on_its_one_canonical_host():
    from cove.search.sources.torrentscsv import ENDPOINT

    source = torrentscsv_source()
    http, session = http_with(FakeResponse(fixture("torrentscsv_valid.json")))
    source.search("example", Category.MOVIES, http)

    assert ENDPOINT == "https://torrents-csv.com/service/search"
    assert session.calls[0][0].startswith("https://torrents-csv.com/")


def test_torrentscsv_errors_never_leak_the_payload_or_a_magnet():
    source = torrentscsv_source()
    http, _ = http_with(FakeResponse(fixture("torrentscsv_unusable.json")))

    with pytest.raises(SourceError) as error:
        source.search("example", Category.MOVIES, http)
    text = str(error.value)
    assert "magnet:" not in text
    assert "infohash" not in text


def test_torrentscsv_never_bypasses_search_http():
    import inspect

    from cove.search.sources import torrentscsv as module

    text = inspect.getsource(module)
    for banned in (
        "import requests",
        "urllib.request",
        "import httpx",
        "import aiohttp",
        "import subprocess",
        "selenium",
        "playwright",
        "BeautifulSoup",
        "lxml",
    ):
        assert banned not in text


# Group L - registration. See tests/test_search_registry.py.
