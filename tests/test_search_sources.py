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

YTS_HOSTS = ("yts.mx", "yts.am", "yts.rs")


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
    assert url.startswith("https://yts.mx/api/v2/list_movies.json")
    assert kwargs["params"]["query_term"] == "harbour lights"


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
    assert [url.split("/")[2] for url, _ in session.calls] == ["yts.mx", "yts.am"]


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
