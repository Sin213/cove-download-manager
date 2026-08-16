"""The generic Torznab search source and its bounded network adapter.

This is Search v2 slice S3: one ``TorznabSource`` built from one
``CustomTorznabIndexer`` record, reusing Cove's existing ``SearchHttp`` and the
S1 protocol parser. Nothing here touches the network for real - every request
goes through a fake session that records what it was asked and replays
deterministic caps/feed bytes.
"""
from pathlib import Path

import pytest
import requests

from cove.search.indexers import CustomTorznabIndexer
from cove.search.magnet import extract_info_hash
from cove.search.models import Category, SourceError, SourceErrorKind
from cove.search.sources.base import SearchHttp, Source
from cove.search.sources.torznab import TorznabSource


FIXTURES = Path(__file__).parent / "fixtures" / "search" / "torznab"

# The persisted identity is authoritative and never derived from the display
# name, endpoint or API key.
INDEXER_ID = "custom:11111111-1111-4111-8111-111111111111"

# A full gateway/per-indexer endpoint path. S3 must preserve it exactly; it
# must not collapse to "/api" or strip gateway/per-indexer segments.
ENDPOINT = "http://127.0.0.1:9117/api/v2.0/indexers/example/results/torznab/api"

# An obvious fake sentinel so a leak is never missed by a test that forgot to
# insert it first.
SECRET = "super-secret-s3-key"

HEX = "c9e15763f722f23e98a29decdfae341b98d53056"
OTHER = "0123456789abcdef0123456789abcdef01234567"

STANDARD_CATEGORIES = [
    (2000, "Movies"),
    (2040, "HD"),
    (5000, "TV"),
    (5040, "HD"),
    (5070, "Anime"),
    (1000, "Console"),
    (4000, "PC"),
]


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- deterministic fake transport (same shape as test_search_sources) -------


class FakeResponse:
    def __init__(self, body: bytes = b"", status_code: int = 200):
        self.body = body
        self.status_code = status_code
        self.headers = {}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)

    def close(self):
        self.closed = True


class LeakingResponse(FakeResponse):
    """A response whose HTTP error text carries the secret-bearing URL, the way
    ``requests`` really does, so sanitization has something concrete to strip."""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} error for url: {ENDPOINT}?apikey={SECRET}",
                response=self,
            )


class FakeSession:
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


# --- deterministic caps / feed builders -------------------------------------


def caps_doc(max_limit=100, default_limit=50, modes=("search", "tv-search", "movie-search"), categories=STANDARD_CATEGORIES):
    limits = f'<limits max="{max_limit}"'
    if default_limit is not None:
        limits += f' default="{default_limit}"'
    limits += "/>"
    searching = "".join(f'<{m} available="yes" supportedParams="q"/>' for m in modes)
    cats = "".join(f'<category id="{cid}" name="{name}"/>' for cid, name in categories)
    cats_section = f"<categories>{cats}</categories>" if categories else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<caps>"
        + limits
        + f"<searching>{searching}</searching>"
        + cats_section
        + "</caps>"
    ).encode()


def caps_response(**kwargs) -> FakeResponse:
    return FakeResponse(caps_doc(**kwargs))


def attr(name: str, value: str) -> str:
    return f'<torznab:attr name="{name}" value="{value}"/>'


def item(inner: str) -> str:
    return f"<item>{inner}</item>"


def feed(*items_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">'
        "<channel>"
        + "".join(items_xml)
        + "</channel></rss>"
    ).encode()


def hash_for(i: int) -> str:
    return f"{i:040x}"


def usable_item(i: int) -> str:
    return item(
        f"<title>Item {i}</title>"
        + attr("infohash", hash_for(i))
        + attr("seeders", "3")
        + attr("leechers", "1")
        + attr("size", "1000")
    )


def download_only_item(i: int) -> str:
    return item(
        f"<title>Download {i}</title>"
        f'<enclosure url="https://example.invalid/d/{i}.torrent" length="10" '
        'type="application/x-bittorrent"/>'
        + attr("size", "10")
    )


def no_identity_item(i: int) -> str:
    return item(f"<title>No Identity {i}</title>" + attr("size", "10"))


def usable_page(start: int, count: int) -> FakeResponse:
    return FakeResponse(feed(*(usable_item(i) for i in range(start, start + count))))


# --- source / helpers -------------------------------------------------------


def make_indexer(**kwargs) -> CustomTorznabIndexer:
    fields = dict(id=INDEXER_ID, enabled=True, name="Test Indexer", url=ENDPOINT, api_key="")
    fields.update(kwargs)
    return CustomTorznabIndexer(**fields)


def make_source(**kwargs) -> TorznabSource:
    return TorznabSource(make_indexer(**kwargs))


def run(source, query, category, *outcomes):
    http, session = http_with(*outcomes)
    return source.search(query, category, http), session


def params_of(session, i):
    return session.calls[i][1]["params"]


def url_of(session, i):
    return session.calls[i][0]


def values(params, key):
    return [v for k, v in params if k == key]


def value(params, key):
    return values(params, key)[0]


# --- RED GROUP 1: source contract / construction ----------------------------


def test_constructed_from_custom_indexer():
    source = TorznabSource(make_indexer())
    assert isinstance(source, Source)
    assert source.id == INDEXER_ID
    assert source.label == "Test Indexer"
    assert source.homepage == ENDPOINT


def test_source_serves_broad_eligibility_before_discovery():
    source = TorznabSource(make_indexer())
    assert source.categories == (Category.MOVIES, Category.TV, Category.ANIME, Category.GAMES)
    assert source.serves(Category.ALL)
    assert source.serves(Category.MOVIES)
    assert source.serves(Category.TV)
    assert source.serves(Category.ANIME)
    assert source.serves(Category.GAMES)


def test_construction_is_side_effect_free():
    indexer = make_indexer()
    first = TorznabSource(indexer)
    second = TorznabSource(indexer)
    assert first.id == second.id == INDEXER_ID
    # The record is consumed, never mutated or replaced.
    assert indexer.id == INDEXER_ID
    assert indexer.url == ENDPOINT


# --- RED GROUP 2: caps request ---------------------------------------------


def test_first_request_uses_full_endpoint_path_and_t_caps():
    _, session = run(make_source(), "x", Category.TV, caps_response(), FakeResponse(feed()))
    assert url_of(session, 0) == ENDPOINT
    assert value(params_of(session, 0), "t") == "caps"


def test_caps_includes_apikey_when_configured():
    _, session = run(make_source(api_key=SECRET), "x", Category.TV, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 0), "apikey") == SECRET


def test_caps_omits_apikey_when_absent():
    _, session = run(make_source(), "x", Category.TV, caps_response(), FakeResponse(feed()))
    assert values(params_of(session, 0), "apikey") == []


def test_unrelated_query_params_preserved():
    source = make_source(url=ENDPOINT + "?foo=bar&baz=qux")
    _, session = run(source, "x", Category.TV, caps_response(), FakeResponse(feed()))
    params = params_of(session, 0)
    assert ("foo", "bar") in params
    assert ("baz", "qux") in params


def test_reserved_query_params_replaced_not_duplicated():
    source = make_source(url=ENDPOINT + "?apikey=old&t=stale&q=oldq&cat=999")
    _, session = run(source, "x", Category.TV, caps_response(), FakeResponse(feed()))
    params = params_of(session, 0)
    assert values(params, "apikey") == []
    assert values(params, "t") == ["caps"]
    assert values(params, "q") == []
    assert values(params, "cat") == []


def test_search_reserved_params_not_duplicated():
    source = make_source(url=ENDPOINT + "?cat=999&limit=1&offset=7")
    _, session = run(source, "x", Category.TV, caps_response(), FakeResponse(feed()))
    params = params_of(session, 1)
    assert len(values(params, "cat")) == 1
    assert len(values(params, "limit")) == 1
    assert len(values(params, "offset")) == 1


# --- RED GROUP 3: search-mode tokens ---------------------------------------


def test_tv_prefers_tvsearch_token():
    _, session = run(make_source(), "x", Category.TV, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "t") == "tvsearch"


def test_tv_falls_back_to_generic_search():
    _, session = run(make_source(), "x", Category.TV, caps_response(modes=("search",)), FakeResponse(feed()))
    assert value(params_of(session, 1), "t") == "search"


def test_movies_prefer_movie_token():
    _, session = run(make_source(), "x", Category.MOVIES, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "t") == "movie"


def test_movies_fall_back_to_generic_search():
    _, session = run(make_source(), "x", Category.MOVIES, caps_response(modes=("search",)), FakeResponse(feed()))
    assert value(params_of(session, 1), "t") == "search"


def test_anime_uses_generic_search():
    _, session = run(make_source(), "x", Category.ANIME, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "t") == "search"


def test_games_use_generic_search():
    _, session = run(make_source(), "x", Category.GAMES, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "t") == "search"


def test_all_uses_generic_search():
    _, session = run(make_source(), "x", Category.ALL, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "t") == "search"


def test_tv_unsupported_returns_empty_without_search_request():
    results, session = run(make_source(), "x", Category.TV, caps_response(modes=("movie-search",)), FakeResponse(feed()))
    assert results == []
    assert len(session.calls) == 1


def test_anime_unsupported_returns_empty_without_search_request():
    results, session = run(make_source(), "x", Category.ANIME, caps_response(modes=("tv-search",)), FakeResponse(feed()))
    assert results == []
    assert len(session.calls) == 1


def test_all_unsupported_returns_empty_without_search_request():
    results, session = run(make_source(), "x", Category.ALL, caps_response(modes=("tv-search", "movie-search")), FakeResponse(feed()))
    assert results == []
    assert len(session.calls) == 1


# --- RED GROUP 4: category requests ----------------------------------------


def test_movies_cat_is_advertised_movie_ids():
    _, session = run(make_source(), "x", Category.MOVIES, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "cat") == "2000,2040"


def test_tv_cat_excludes_anime():
    _, session = run(make_source(), "x", Category.TV, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "cat") == "5000,5040"


def test_anime_cat_retains_anime_specificity():
    _, session = run(make_source(), "x", Category.ANIME, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "cat") == "5070"


def test_games_cat_is_advertised_game_ids():
    _, session = run(make_source(), "x", Category.GAMES, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "cat") == "1000,4000"


def test_all_omits_cat():
    _, session = run(make_source(), "x", Category.ALL, caps_response(), FakeResponse(feed()))
    assert values(params_of(session, 1), "cat") == []


def test_absent_category_returns_empty_without_search_request():
    results, session = run(
        make_source(),
        "x",
        Category.GAMES,
        caps_response(categories=[(2000, "Movies"), (5000, "TV")]),
        FakeResponse(feed()),
    )
    assert results == []
    assert len(session.calls) == 1


# --- RED GROUP 5: multi-word query -----------------------------------------


def test_multi_word_query_is_one_q_value():
    _, session = run(make_source(), "Breaking Bad", Category.TV, caps_response(), FakeResponse(feed()))
    assert values(params_of(session, 1), "q") == ["Breaking Bad"]


# --- RED GROUP 6: basic result conversion ----------------------------------


def test_result_conversion_infohash_only():
    results, _ = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        FakeResponse(feed(item(
            "<title>Alpha</title>"
            + attr("infohash", HEX)
            + attr("size", "500")
            + attr("seeders", "9")
            + attr("leechers", "2")
            + attr("category", "5000")
        ))),
    )
    assert len(results) == 1
    row = results[0]
    assert row.info_hash == HEX
    assert row.name == "Alpha"
    assert extract_info_hash(row.magnet) == HEX
    assert row.size_bytes == 500
    assert row.seeders == 9
    assert row.leechers == 2
    assert row.source == INDEXER_ID


def test_result_conversion_magnet_only():
    magnet = f"magnet:?xt=urn:btih:{HEX}&amp;dn=Bravo"
    results, _ = run(
        make_source(),
        "x",
        Category.MOVIES,
        caps_response(),
        FakeResponse(feed(item(
            "<title>Bravo</title>" + attr("magneturl", magnet) + attr("category", "2000")
        ))),
    )
    assert len(results) == 1
    assert results[0].info_hash == HEX
    assert extract_info_hash(results[0].magnet) == HEX


def test_result_conversion_matching_dual_identity():
    results, _ = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        FakeResponse(feed(item(
            "<title>Both</title>"
            + attr("infohash", HEX)
            + attr("magneturl", f"magnet:?xt=urn:btih:{HEX}&amp;dn=Both")
            + attr("category", "5000")
        ))),
    )
    assert len(results) == 1
    assert results[0].info_hash == HEX


def test_result_conversion_pubdate_is_utc_epoch():
    results, _ = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        FakeResponse(feed(item(
            "<title>Dated</title>"
            "<pubDate>Sat, 05 Aug 2023 12:34:56 +0000</pubDate>"
            + attr("infohash", HEX)
            + attr("category", "5000")
        ))),
    )
    assert results[0].added == 1691238896


def test_result_source_uses_persisted_custom_id():
    results, _ = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        FakeResponse(feed(item("<title>One</title>" + attr("infohash", HEX)))),
    )
    assert results[0].source == INDEXER_ID


def test_reuses_s1_caps_and_feed_fixtures():
    results, _ = run(
        make_source(),
        "Breaking Bad",
        Category.TV,
        FakeResponse(fixture("caps-basic.xml")),
        FakeResponse(fixture("feed-basic.xml")),
    )
    assert len(results) == 2
    assert {r.info_hash for r in results} == {HEX, OTHER}
    assert all(r.source == INDEXER_ID for r in results)


# --- RED GROUP 7: unusable identity ----------------------------------------


def test_download_only_discarded_without_extra_request():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        FakeResponse(feed(
            item("<title>Usable</title>" + attr("infohash", HEX)),
            item(
                "<title>Download Only</title>"
                '<enclosure url="https://example.invalid/d/9.torrent" length="10" '
                'type="application/x-bittorrent"/>'
                + attr("size", "10")
            ),
            item("<title>Usable Two</title>" + attr("infohash", OTHER)),
        )),
    )
    assert [r.name for r in results] == ["Usable", "Usable Two"]
    assert len(session.calls) == 2  # caps + one page, no enclosure fetch


def test_no_identity_discarded():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        FakeResponse(feed(
            item("<title>No Identity</title>" + attr("size", "10")),
            item("<title>Usable</title>" + attr("infohash", HEX)),
        )),
    )
    assert [r.name for r in results] == ["Usable"]
    assert len(session.calls) == 2


# --- RED GROUP 8: pagination basics ----------------------------------------


def test_page1_full_requests_page2():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        usable_page(1, 50),
        usable_page(51, 5),
    )
    assert len(session.calls) == 3
    assert len(results) == 55
    assert value(params_of(session, 2), "offset") == "50"


def test_two_full_pages_request_page3():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        usable_page(1, 50),
        usable_page(51, 50),
        usable_page(101, 3),
    )
    assert len(session.calls) == 4
    assert len(results) == 103
    assert value(params_of(session, 3), "offset") == "100"


def test_three_full_pages_no_page4():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        usable_page(1, 50),
        usable_page(51, 50),
        usable_page(101, 50),
    )
    assert len(session.calls) == 4
    assert len(results) == 150


def test_offset_starts_at_zero():
    _, session = run(make_source(), "x", Category.TV, caps_response(), FakeResponse(feed()))
    assert value(params_of(session, 1), "offset") == "0"


def test_limit_uses_caps_default():
    _, session = run(make_source(), "x", Category.TV, caps_response(default_limit=30), FakeResponse(feed()))
    assert value(params_of(session, 1), "limit") == "30"


def test_limit_falls_back_to_fifty_when_no_default():
    _, session = run(make_source(), "x", Category.TV, caps_response(default_limit=None), FakeResponse(feed()))
    assert value(params_of(session, 1), "limit") == "50"


def test_limit_falls_back_when_default_zero():
    _, session = run(make_source(), "x", Category.TV, caps_response(default_limit=0), FakeResponse(feed()))
    assert value(params_of(session, 1), "limit") == "50"


def test_limit_clamped_to_cove_max_results():
    _, session = run(make_source(), "x", Category.TV, caps_response(default_limit=500, max_limit=1000), FakeResponse(feed()))
    assert value(params_of(session, 1), "limit") == "200"


def test_short_page_stops():
    results, session = run(make_source(), "x", Category.TV, caps_response(), usable_page(1, 25))
    assert len(results) == 25
    assert len(session.calls) == 2  # caps + one page, no page 2


def test_empty_page_stops():
    results, session = run(make_source(), "x", Category.TV, caps_response(), FakeResponse(feed()))
    assert results == []
    assert len(session.calls) == 2


# --- RED GROUP 9: filtered-item offset -------------------------------------


def test_offset_advances_by_raw_not_usable_count():
    page1 = FakeResponse(feed(
        *(usable_item(i) for i in range(1, 41)),
        *(download_only_item(i) for i in range(41, 51)),
    ))
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        page1,
        usable_page(51, 5),
    )
    assert len(results) == 45  # 40 usable + 5 usable
    assert value(params_of(session, 2), "offset") == "50"


# --- RED GROUP 10: raw result budget ---------------------------------------


def test_raw_budget_final_page_limit_shrinks():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(default_limit=75, max_limit=75),
        usable_page(1, 75),
        usable_page(76, 75),
        usable_page(151, 50),
    )
    assert value(params_of(session, 3), "limit") == "50"
    assert len(results) == 200
    assert len(session.calls) == 4


def test_raw_budget_stops_before_fourth_page():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(default_limit=100, max_limit=100),
        usable_page(1, 100),
        usable_page(101, 100),
    )
    assert len(results) == 200
    assert len(session.calls) == 3  # caps + two pages; budget exhausted


def test_unusable_rows_consume_raw_budget():
    page1 = FakeResponse(feed(*(download_only_item(i) for i in range(1, 101))))
    page2 = FakeResponse(feed(*(download_only_item(i) for i in range(101, 201))))
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(default_limit=100, max_limit=100),
        page1,
        page2,
    )
    assert results == []
    assert len(session.calls) == 3  # unusable rows still consumed the budget


def test_malformed_items_do_not_stop_pagination_or_shorten_offset():
    # 45 usable + 5 malformed (dropped by the parser) = 50 raw items. Parsed
    # count alone would read this as a short page and stop early; the raw count
    # must keep pagination going and advance the offset by the full 50.
    page1 = FakeResponse(feed(
        *(usable_item(i) for i in range(1, 46)),
        *("<item/>" for _ in range(5)),
    ))
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        page1,
        usable_page(51, 5),
    )
    assert len(results) == 50  # 45 usable on page 1 + 5 usable on page 2
    assert value(params_of(session, 2), "offset") == "50"
    assert len(session.calls) == 3  # caps + page 1 + page 2, not stopped early


def test_malformed_items_consume_raw_budget():
    # Each page has 50 usable + 50 malformed = 100 raw items. The budget must
    # exhaust after two pages even though only 100 rows are usable.
    page1 = FakeResponse(feed(
        *(usable_item(i) for i in range(1, 51)),
        *("<item/>" for _ in range(50)),
    ))
    page2 = FakeResponse(feed(
        *(usable_item(i) for i in range(51, 101)),
        *("<item/>" for _ in range(50)),
    ))
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(default_limit=100, max_limit=100),
        page1,
        page2,
    )
    assert len(results) == 100
    assert len(session.calls) == 3  # caps + two pages; raw budget exhausted


def test_fully_malformed_page_does_not_stop_pagination():
    # A full page consisting entirely of malformed items yields zero parsed
    # rows, but the server's raw window is full (50 raw items). An empty parsed
    # tuple must not be read as end-of-feed; the raw count keeps the paging
    # window alive, advances the offset by 50, and fetches the next page.
    page1 = FakeResponse(feed(*("<item/>" for _ in range(50))))
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        page1,
        usable_page(51, 5),
    )
    assert len(results) == 5
    assert value(params_of(session, 2), "offset") == "50"
    assert len(session.calls) == 3  # caps + malformed page + valid page


def test_consecutive_malformed_pages_do_not_fake_repeated_page():
    # Two full pages of entirely malformed items both produce an empty parsed
    # signature. They must not be mistaken for a repeated page, or valid results
    # on a later page would be silently dropped.
    page1 = FakeResponse(feed(*("<item/>" for _ in range(50))))
    page2 = FakeResponse(feed(*("<item/>" for _ in range(50))))
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        page1,
        page2,
        usable_page(101, 5),
    )
    assert len(results) == 5
    assert value(params_of(session, 3), "offset") == "100"
    assert len(session.calls) == 4  # caps + two malformed pages + valid page


def test_oversized_page_is_truncated_before_parsing(monkeypatch):
    from cove.search import torznab as protocol
    from cove.search.sources import torznab as source_module

    parsed_counts = []
    real_parse = protocol.parse_search_feed

    def counting_parse(raw: bytes):
        parsed_counts.append(source_module._count_feed_items(raw))
        return real_parse(raw)

    monkeypatch.setattr(source_module, "parse_search_feed", counting_parse)

    # A noncompliant endpoint ignores the 50-item limit and returns 250 items in
    # one page. The source must bound its work to the page it actually asked
    # for, not let the parser chew through every returned row.
    oversized = FakeResponse(feed(*(usable_item(i) for i in range(1, 251))))
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        oversized,
    )
    assert len(results) == 50
    assert parsed_counts == [50]  # parser only ever saw the truncated 50-item feed
    assert len(session.calls) == 2  # caps + one page; raw budget exhausted at 250


def test_raw_count_guard_rejects_entity_before_expansion():
    # The source re-reads the feed (raw count / truncation) before the S1 parser
    # runs its own guard, so that re-read must reject a DTD/entity itself. If it
    # did not, ElementTree would expand the internal entity before the parser
    # ever saw the bytes.
    from cove.search.sources.torznab import _count_feed_items

    hostile = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE rss [<!ENTITY boom "BOMB">]>'
        b"<rss><channel><item><title>&boom;</title></item></channel></rss>"
    )
    assert _count_feed_items(hostile) == 0


# --- RED GROUP 11: repeated page -------------------------------------------


def test_repeated_page_stops_without_third_request():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(default_limit=5, max_limit=5),
        usable_page(1, 5),
        usable_page(1, 5),
    )
    assert len(results) == 5  # first page kept once
    assert len(session.calls) == 3  # caps + page1 + page2, no page3


# --- RED GROUP 12: source request budget -----------------------------------


def test_source_request_budget_is_four_calls_max():
    results, session = run(
        make_source(),
        "x",
        Category.TV,
        caps_response(),
        usable_page(1, 50),
        usable_page(51, 50),
        usable_page(101, 50),
    )
    assert len(session.calls) == 4
    assert len(results) == 150
    assert value(params_of(session, 0), "t") == "caps"
    for i in (1, 2, 3):
        assert value(params_of(session, i), "t") == "tvsearch"


# --- RED GROUP 13: failure classification ----------------------------------


def test_network_error_classified():
    http, _ = http_with(caps_response(), requests.ConnectionError("boom"), requests.ConnectionError("boom"))
    with pytest.raises(SourceError) as excinfo:
        make_source().search("x", Category.TV, http)
    assert excinfo.value.kind is SourceErrorKind.NETWORK


def test_timeout_classified():
    http, _ = http_with(caps_response(), requests.Timeout("slow"), requests.Timeout("slow"))
    with pytest.raises(SourceError) as excinfo:
        make_source().search("x", Category.TV, http)
    assert excinfo.value.kind is SourceErrorKind.TIMEOUT


def test_http_500_classified():
    http, _ = http_with(caps_response(), FakeResponse(b"{}", status_code=500))
    with pytest.raises(SourceError) as excinfo:
        make_source().search("x", Category.TV, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP


def test_http_401_classified_as_authentication():
    http, _ = http_with(caps_response(), FakeResponse(b"{}", status_code=401))
    with pytest.raises(SourceError) as excinfo:
        make_source().search("x", Category.TV, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP
    assert str(excinfo.value) == "Torznab authentication failed"


def test_http_403_classified_as_authentication():
    http, _ = http_with(caps_response(), FakeResponse(b"{}", status_code=403))
    with pytest.raises(SourceError) as excinfo:
        make_source().search("x", Category.TV, http)
    assert excinfo.value.kind is SourceErrorKind.HTTP
    assert str(excinfo.value) == "Torznab authentication failed"


def test_malformed_caps_classified_as_parse():
    http, _ = http_with(FakeResponse(b"not xml"))
    with pytest.raises(SourceError) as excinfo:
        make_source().search("x", Category.TV, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_malformed_feed_classified_as_parse():
    http, _ = http_with(caps_response(), FakeResponse(b"not xml"))
    with pytest.raises(SourceError) as excinfo:
        make_source().search("x", Category.TV, http)
    assert excinfo.value.kind is SourceErrorKind.PARSE


def test_zero_results_returns_empty_list():
    results, _ = run(make_source(), "x", Category.TV, caps_response(), FakeResponse(feed()))
    assert results == []


# --- RED GROUP 14: secret leakage ------------------------------------------


def test_secret_reaches_outgoing_requests():
    _, session = run(make_source(api_key=SECRET), "Breaking Bad", Category.TV, caps_response(), FakeResponse(feed()))
    assert ("apikey", SECRET) in params_of(session, 0)
    assert ("apikey", SECRET) in params_of(session, 1)


def test_secret_absent_from_auth_error():
    http, session = http_with(caps_response(), LeakingResponse(status_code=401))
    with pytest.raises(SourceError) as excinfo:
        make_source(api_key=SECRET).search("Breaking Bad", Category.TV, http)
    assert ("apikey", SECRET) in session.calls[1][1]["params"]
    error = excinfo.value
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert SECRET not in " ".join(map(str, error.args))
    assert str(error) == "Torznab authentication failed"


def test_secret_absent_from_http_error():
    http, session = http_with(caps_response(), LeakingResponse(status_code=500))
    with pytest.raises(SourceError) as excinfo:
        make_source(api_key=SECRET).search("Breaking Bad", Category.TV, http)
    assert ("apikey", SECRET) in session.calls[1][1]["params"]
    error = excinfo.value
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert str(error) == "Torznab request failed"


def test_secret_absent_from_network_error():
    http, session = http_with(
        caps_response(),
        requests.ConnectionError(f"failed for {ENDPOINT}?apikey={SECRET}"),
        requests.ConnectionError(f"failed for {ENDPOINT}?apikey={SECRET}"),
    )
    with pytest.raises(SourceError) as excinfo:
        make_source(api_key=SECRET).search("Breaking Bad", Category.TV, http)
    assert ("apikey", SECRET) in session.calls[1][1]["params"]
    error = excinfo.value
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert error.kind is SourceErrorKind.NETWORK


def test_secret_absent_from_parse_failure():
    http, session = http_with(caps_response(), FakeResponse(b"not xml"))
    with pytest.raises(SourceError) as excinfo:
        make_source(api_key=SECRET).search("Breaking Bad", Category.TV, http)
    assert ("apikey", SECRET) in session.calls[1][1]["params"]
    error = excinfo.value
    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert error.kind is SourceErrorKind.PARSE


# --- RED GROUP 15: current transport reuse ---------------------------------


def test_source_module_has_approved_dependencies_only():
    import inspect

    import cove.search.sources.torznab as mod

    src = inspect.getsource(mod)
    for forbidden in (
        "import requests",
        "from requests",
        "urllib.request",
        "import socket",
        "import httpx",
        "import aiohttp",
        "cove.search.service",
        "cove.search.registry",
        "PySide6",
    ):
        assert forbidden not in src, f"forbidden dependency in torznab source: {forbidden}"


def test_search_flows_through_searchhttp():
    results, session = run(make_source(), "x", Category.TV, caps_response(), FakeResponse(feed(item("<title>One</title>" + attr("infohash", HEX)))))
    assert len(results) == 1
    assert len(session.calls) >= 2  # caps + search page, both via SearchHttp


# --- probe_caps (Test Connection backend, S6) -------------------------------


def _probe(source, monkeypatch, *outcomes):
    """Run probe_caps against a fake SearchHttp factory wrapping FakeSession."""
    session = FakeSession(*outcomes)
    monkeypatch.setattr(
        "cove.search.sources.torznab.SearchHttp",
        lambda interface: SearchHttp(interface, session=session),
    )
    return source.probe_caps(), session


def test_probe_caps_performs_exactly_one_caps_request(monkeypatch):
    caps, session = _probe(make_source(api_key=SECRET), monkeypatch, caps_response())
    assert caps.search_modes == ("search", "tv-search", "movie-search")
    assert len(session.calls) == 1
    assert url_of(session, 0) == ENDPOINT
    params = params_of(session, 0)
    assert ("t", "caps") in params
    assert ("apikey", SECRET) in params
    # Zero content-search requests: no q/limit/offset ever appears.
    assert values(params, "q") == []
    assert values(params, "limit") == []
    assert values(params, "offset") == []


def test_probe_caps_passes_the_requested_interface(monkeypatch):
    seen = {}
    session = FakeSession(caps_response())

    def factory(interface):
        seen["interface"] = interface
        return SearchHttp(interface, session=session)

    monkeypatch.setattr("cove.search.sources.torznab.SearchHttp", factory)
    make_source().probe_caps(interface="eth0")
    assert seen["interface"] == "eth0"


def test_probe_caps_rejects_public_http_before_transport(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(
        "cove.search.sources.torznab.SearchHttp",
        lambda interface: SearchHttp(interface, session=session),
    )
    source = make_source(url="http://example.com/api", api_key=SECRET)
    with pytest.raises(SourceError) as excinfo:
        source.probe_caps()
    assert "HTTPS" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)
    assert session.calls == []  # rejected before any request


def test_probe_caps_allows_public_https(monkeypatch):
    caps, session = _probe(
        make_source(url="https://example.com/api"), monkeypatch, caps_response()
    )
    assert len(session.calls) == 1


def test_probe_caps_sanitizes_auth_failure(monkeypatch):
    source = make_source(api_key=SECRET)
    with pytest.raises(SourceError) as excinfo:
        _probe(source, monkeypatch, LeakingResponse(status_code=401))
    assert str(excinfo.value) == "Torznab authentication failed"
    assert SECRET not in str(excinfo.value)


def test_probe_caps_sanitizes_malformed_caps(monkeypatch):
    source = make_source(api_key=SECRET)
    with pytest.raises(SourceError) as excinfo:
        _probe(source, monkeypatch, FakeResponse(b"<caps><nope/></caps>"))
    assert "caps is not usable" in str(excinfo.value)
    assert SECRET not in str(excinfo.value)
