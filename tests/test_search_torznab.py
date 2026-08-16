"""Torznab capability and search-feed parsing.

Torznab is the extensibility boundary for Search v2: a deterministic, bounded,
network-free parser that turns a server's ``caps`` document and its RSS search
response into Cove-normalised intermediate records. Nothing here touches the
network - every document is bytes - and every untrusted input is bounded so a
hostile indexer cannot turn XML into memory pressure.
"""
from pathlib import Path

import pytest

from cove.search.magnet import build_magnet, extract_info_hash
from cove.search.models import Category
from cove.search.torznab import (
    MAX_DOCUMENT_BYTES,
    MAX_FIELD_LENGTH,
    MAX_ITEMS,
    TorznabCaps,
    TorznabCategory,
    TorznabIdentity,
    TorznabItem,
    TorznabParseError,
    map_torznab_category,
    parse_caps,
    parse_search_feed,
)


FIXTURES = Path(__file__).parent / "fixtures" / "search" / "torznab"

HEX = "c9e15763f722f23e98a29decdfae341b98d53056"
OTHER = "0123456789abcdef0123456789abcdef01234567"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def feed(*item_xml: str) -> bytes:
    """A Torznab RSS document wrapping `item_xml` items."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed">'
        "<channel>"
        + "".join(item_xml)
        + "</channel></rss>"
    ).encode()


def attr(name: str, value: str) -> str:
    return f'<torznab:attr name="{name}" value="{value}"/>'


def item(inner: str) -> str:
    return f"<item>{inner}</item>"


# --- RED GROUP 1: module / caps -------------------------------------------

def test_valid_caps_are_parsed():
    caps = parse_caps(fixture("caps-basic.xml"))
    assert isinstance(caps, TorznabCaps)
    assert caps.max_limit == 100
    assert caps.default_limit == 50


def test_caps_search_modes_exclude_unavailable_and_unknown():
    caps = parse_caps(fixture("caps-basic.xml"))
    # audio-search is advertised but available="no", so only torrent modes survive.
    assert caps.search_modes == ("search", "tv-search", "movie-search")


def test_tv_search_only_endpoint_remains_usable():
    caps = parse_caps(fixture("caps-tv-only.xml"))
    assert caps.search_modes == ("tv-search",)


def test_movie_search_only_endpoint_remains_usable():
    caps = parse_caps(fixture("caps-movie-only.xml"))
    assert caps.search_modes == ("movie-search",)


def test_caps_categories_are_flattened_with_names():
    caps = parse_caps(fixture("caps-basic.xml"))
    ids = {c.id for c in caps.categories}
    assert {2000, 2040, 5000, 5040, 5070, 1000, 4000} <= ids
    by_id = {c.id: c for c in caps.categories}
    assert by_id[5070].name == "Anime"
    assert by_id[2000].name == "Movies"


def test_caps_reject_not_xml():
    with pytest.raises(TorznabParseError):
        parse_caps(b"this is not xml")


def test_caps_reject_unsupported_encoding():
    # ElementTree raises LookupError for an unknown declared encoding; it must
    # not escape the public TorznabParseError contract.
    with pytest.raises(TorznabParseError):
        parse_caps(b'<?xml version="1.0" encoding="bogus"?><caps></caps>')


def test_feed_reject_unsupported_encoding():
    with pytest.raises(TorznabParseError):
        parse_search_feed(b'<?xml version="1.0" encoding="bogus"?><rss></rss>')


def test_caps_reject_wrong_root():
    with pytest.raises(TorznabParseError):
        parse_caps(b"<rss><channel/></rss>")


def test_caps_reject_no_searching_section():
    with pytest.raises(TorznabParseError):
        parse_caps(b"<caps><limits max='10' default='5'/></caps>")


def test_caps_reject_no_usable_search_mode():
    with pytest.raises(TorznabParseError):
        parse_caps(fixture("caps-no-search.xml"))


def test_caps_reject_invalid_numeric_limit():
    with pytest.raises(TorznabParseError):
        parse_caps(b"<caps><limits max='nope' default='5'/><searching><search/></searching></caps>")


def test_caps_reject_negative_limit():
    with pytest.raises(TorznabParseError):
        parse_caps(b"<caps><limits max='-1' default='5'/><searching><search/></searching></caps>")


def test_caps_reject_default_above_max():
    with pytest.raises(TorznabParseError):
        parse_caps(b"<caps><limits max='10' default='50'/><searching><search/></searching></caps>")


def test_caps_reject_invalid_category_id():
    with pytest.raises(TorznabParseError):
        parse_caps(
            b"<caps><limits max='10' default='5'/><searching><search/></searching>"
            b"<categories><category id='abc' name='Bad'/></categories></caps>"
        )


def test_caps_reject_missing_max_limit():
    with pytest.raises(TorznabParseError):
        parse_caps(b"<caps><limits default='5'/><searching><search/></searching></caps>")


# --- RED GROUP 2: category mapping ----------------------------------------

def test_map_movies_parent():
    assert map_torznab_category(2000) is Category.MOVIES


def test_map_movies_subcategory():
    assert map_torznab_category(2040) is Category.MOVIES


def test_map_tv_parent():
    assert map_torznab_category(5000) is Category.TV


def test_map_tv_subcategory():
    assert map_torznab_category(5040) is Category.TV


def test_map_anime_specificity_beats_tv_family():
    assert map_torznab_category(5070) is Category.ANIME
    assert map_torznab_category(5070) is not Category.TV


def test_map_games_console_family():
    assert map_torznab_category(1000) is Category.GAMES
    assert map_torznab_category(1010) is Category.GAMES


def test_map_games_pc_family():
    assert map_torznab_category(4000) is Category.GAMES
    assert map_torznab_category(4010) is Category.GAMES


def test_map_unknown_category_is_none():
    assert map_torznab_category(3000) is None
    assert map_torznab_category(6000) is None
    assert map_torznab_category(9999) is None
    assert map_torznab_category(-1) is None


# --- RED GROUP 3: search feed ---------------------------------------------

def test_feed_parses_multiple_items():
    results = parse_search_feed(fixture("feed-basic.xml"))
    assert len(results) == 2
    first = results[0]
    assert first.title == "Example Movie 2020 1080p"
    assert first.size_bytes == 1468006400
    assert first.seeders == 123
    assert first.leechers == 4
    assert first.category_ids == (2000, 2040)
    assert first.guid == "https://example.invalid/view/1"
    assert first.published_at == 1691238896
    assert first.enclosure_url == "https://example.invalid/download/1.torrent"


def test_feed_normalises_uppercase_infohash():
    results = parse_search_feed(fixture("feed-basic.xml"))
    assert results[1].info_hash == OTHER


def test_feed_alternate_namespace_prefix():
    results = parse_search_feed(fixture("feed-alternate-prefix.xml"))
    assert len(results) == 1
    assert results[0].info_hash == HEX
    assert results[0].size_bytes == 1024
    assert results[0].seeders == 3


def test_feed_missing_optional_metadata():
    results = parse_search_feed(
        feed(
            item(
                "<title>Bare</title>"
                + attr("category", "2000")
                + attr("infohash", HEX)
            )
        )
    )
    assert len(results) == 1
    row = results[0]
    assert row.size_bytes is None
    assert row.seeders == 0
    assert row.leechers == 0
    assert row.guid is None
    assert row.published_at is None
    assert row.enclosure_url is None


def test_pubdate_without_timezone_is_utc_deterministic():
    # RFC 822 "-0000" and bare dates parse to timezone-naive datetimes; they
    # must be interpreted as UTC so the epoch never depends on host timezone.
    expected = 1691238896  # 2023-08-05 12:34:56 UTC
    for text in ("Sat, 05 Aug 2023 12:34:56", "Sat, 05 Aug 2023 12:34:56 -0000"):
        results = parse_search_feed(
            feed(
                item(
                    "<title>Dated</title>"
                    f"<pubDate>{text}</pubDate>"
                    + attr("category", "2000")
                    + attr("infohash", HEX)
                )
            )
        )
        assert results[0].published_at == expected


def test_feed_ignores_unknown_non_load_bearing_elements():
    results = parse_search_feed(
        feed(
            item(
                "<title>Extra</title>"
                + "<foo:bar xmlns:foo='urn:other'>ignored</foo:bar>"
                + attr("infohash", HEX)
                + attr("size", "10")
                + attr("seeders", "2")
            )
        )
    )
    assert len(results) == 1
    assert results[0].title == "Extra"


def test_feed_duplicate_rows_stay_separate():
    row = item(
        "<title>Dup</title>" + attr("infohash", HEX) + attr("size", "10")
    )
    results = parse_search_feed(feed(row, row))
    assert len(results) == 2
    assert results[0].info_hash == results[1].info_hash == HEX


def test_feed_unparseable_optional_size_is_none():
    results = parse_search_feed(
        feed(item("<title>Bad Size</title>" + attr("size", "huge") + attr("infohash", HEX)))
    )
    assert results[0].size_bytes is None


def test_feed_negative_size_is_none():
    results = parse_search_feed(
        feed(item("<title>Neg Size</title>" + attr("size", "-5") + attr("infohash", HEX)))
    )
    assert results[0].size_bytes is None


def test_feed_negative_swarm_count_becomes_default():
    results = parse_search_feed(
        feed(
            item(
                "<title>Neg Swarm</title>"
                + attr("seeders", "-9")
                + attr("leechers", "-1")
                + attr("infohash", HEX)
            )
        )
    )
    assert len(results) == 1
    assert results[0].seeders == 0
    assert results[0].leechers == 0


# --- RED GROUP 4: torrent identity ----------------------------------------

def test_infohash_only_produces_normalised_hash_and_magnet():
    results = parse_search_feed(
        feed(item("<title>Hash Only</title>" + attr("infohash", HEX.upper())))
    )
    assert results[0].identity is TorznabIdentity.USABLE_MAGNET_IDENTITY
    assert results[0].info_hash == HEX
    assert extract_info_hash(results[0].magnet) == HEX


def test_magnet_only_extracts_hash():
    results = parse_search_feed(fixture("feed-magnet-only.xml"))
    row = results[0]
    assert row.identity is TorznabIdentity.USABLE_MAGNET_IDENTITY
    assert row.info_hash == HEX
    assert extract_info_hash(row.magnet) == HEX


def test_matching_infohash_and_magnet_accepted():
    results = parse_search_feed(
        feed(
            item(
                "<title>Both</title>"
                + attr("infohash", HEX)
                + attr("magneturl", f"magnet:?xt=urn:btih:{HEX}&amp;dn=Both")
            )
        )
    )
    assert len(results) == 1
    assert results[0].identity is TorznabIdentity.USABLE_MAGNET_IDENTITY
    assert results[0].info_hash == HEX


def test_magnet_enclosure_produces_usable_identity():
    # Torznab permits the RSS enclosure itself to carry a magnet URI, not only
    # a .torrent URL. Such an enclosure is a usable magnet identity, not a
    # torrent-download-only row.
    magnet_uri = f"magnet:?xt=urn:btih:{HEX}&amp;dn=Enclosure+Magnet"
    results = parse_search_feed(
        feed(
            item(
                "<title>Enclosure Magnet</title>"
                f'<enclosure url="{magnet_uri}" length="10" '
                'type="application/x-bittorrent;x-scheme-handler/magnet"/>'
            )
        )
    )
    assert len(results) == 1
    row = results[0]
    assert row.identity is TorznabIdentity.USABLE_MAGNET_IDENTITY
    assert row.info_hash == HEX
    assert extract_info_hash(row.magnet) == HEX
    # The raw enclosure is preserved as opaque text alongside the identity.
    assert row.enclosure_url.startswith("magnet:?xt=urn:btih:")


def test_magnet_enclosure_conflicting_with_infohash_rejected():
    magnet_uri = f"magnet:?xt=urn:btih:{HEX}&amp;dn=Conflict"
    results = parse_search_feed(
        feed(
            item(
                "<title>Conflict</title>"
                + attr("infohash", OTHER)
                + f'<enclosure url="{magnet_uri}" length="10" type="application/x-bittorrent"/>'
            )
        )
    )
    assert results == ()


def test_uppercase_magnet_scheme_enclosure_is_usable():
    # URI schemes are case-insensitive; a MAGNET: enclosure is still a magnet.
    magnet_uri = f"MAGNET:?xt=urn:btih:{HEX}&amp;dn=Upper"
    results = parse_search_feed(
        feed(
            item(
                "<title>Upper Magnet</title>"
                f'<enclosure url="{magnet_uri}" length="10" '
                'type="application/x-bittorrent;x-scheme-handler/magnet"/>'
            )
        )
    )
    assert len(results) == 1
    assert results[0].identity is TorznabIdentity.USABLE_MAGNET_IDENTITY
    assert results[0].info_hash == HEX


def test_uppercase_magnet_scheme_magneturl_is_usable():
    results = parse_search_feed(
        feed(
            item(
                "<title>Upper Magneturl</title>"
                + attr("magneturl", f"MAGNET:?xt=urn:btih:{HEX}&amp;dn=Upper")
            )
        )
    )
    assert len(results) == 1
    assert results[0].identity is TorznabIdentity.USABLE_MAGNET_IDENTITY
    assert results[0].info_hash == HEX


def test_malformed_magnet_enclosure_is_not_download_only():
    # A magnet-scheme enclosure with no usable BTIH is neither a .torrent
    # download nor a usable magnet: it must not enter the torrent-download path.
    results = parse_search_feed(
        feed(
            item(
                "<title>Broken Magnet</title>"
                '<enclosure url="magnet:?dn=no-btih" length="10" '
                'type="application/x-bittorrent;x-scheme-handler/magnet"/>'
            )
        )
    )
    assert len(results) == 1
    row = results[0]
    assert row.identity is TorznabIdentity.NO_USABLE_IDENTITY
    assert row.enclosure_url is None
    assert row.info_hash is None
    assert row.magnet is None


def test_conflicting_infohash_and_magnet_rejected():
    results = parse_search_feed(fixture("feed-conflicting-hash.xml"))
    assert results == ()


def test_malformed_infohash_does_not_escape_as_valid():
    results = parse_search_feed(
        feed(item("<title>Bad Hash</title>" + attr("infohash", "not-a-hash")))
    )
    assert len(results) == 1
    assert results[0].identity is not TorznabIdentity.USABLE_MAGNET_IDENTITY
    assert results[0].info_hash is None
    assert results[0].magnet is None


def test_malformed_magnet_does_not_escape_as_valid():
    results = parse_search_feed(
        feed(item("<title>Bad Magnet</title>" + attr("magneturl", "http://not-a-magnet/")))
    )
    assert len(results) == 1
    assert results[0].identity is not TorznabIdentity.USABLE_MAGNET_IDENTITY
    assert results[0].info_hash is None
    assert results[0].magnet is None


def test_download_only_when_enclosure_present():
    results = parse_search_feed(fixture("feed-download-only.xml"))
    row = results[0]
    assert row.identity is TorznabIdentity.TORRENT_DOWNLOAD_ONLY
    assert row.info_hash is None
    assert row.magnet is None
    assert row.enclosure_url == "https://example.invalid/download/9.torrent"


def test_no_usable_identity_when_nothing_present():
    results = parse_search_feed(
        feed(item("<title>Nothing</title>" + attr("size", "10")))
    )
    row = results[0]
    assert row.identity is TorznabIdentity.NO_USABLE_IDENTITY
    assert row.info_hash is None
    assert row.magnet is None
    assert row.enclosure_url is None


def test_download_only_and_no_identity_are_distinct_states():
    download_only = parse_search_feed(fixture("feed-download-only.xml"))[0]
    no_identity = parse_search_feed(
        feed(item("<title>Nothing</title>" + attr("size", "10")))
    )[0]
    assert download_only.identity is not no_identity.identity


def test_torrent_download_only_requires_enclosure_url():
    with pytest.raises(ValueError):
        TorznabItem(
            title="t",
            size_bytes=None,
            seeders=0,
            leechers=0,
            info_hash=None,
            magnet=None,
            enclosure_url=None,
            category_ids=(),
            guid=None,
            published_at=None,
            identity=TorznabIdentity.TORRENT_DOWNLOAD_ONLY,
        )


def test_no_usable_identity_rejects_enclosure_url():
    with pytest.raises(ValueError):
        TorznabItem(
            title="t",
            size_bytes=None,
            seeders=0,
            leechers=0,
            info_hash=None,
            magnet=None,
            enclosure_url="https://example.invalid/x.torrent",
            category_ids=(),
            guid=None,
            published_at=None,
            identity=TorznabIdentity.NO_USABLE_IDENTITY,
        )


# --- RED GROUP 5: item isolation ------------------------------------------

def test_malformed_item_does_not_destroy_valid_items():
    results = parse_search_feed(
        feed(
            item("<title>Good</title>" + attr("infohash", HEX)),
            item("<title>   </title>" + attr("infohash", HEX)),
            item("<title>No Identity</title>" + attr("size", "10")),
            item("<title>Good Two</title>" + attr("infohash", OTHER)),
        )
    )
    # The empty-title row is dropped; the identity-less row is preserved as
    # NO_USABLE_IDENTITY and must not disturb the two usable rows around it.
    assert [r.title for r in results] == ["Good", "No Identity", "Good Two"]
    assert results[1].identity is TorznabIdentity.NO_USABLE_IDENTITY


def test_identity_conflict_item_does_not_destroy_valid_items():
    results = parse_search_feed(
        feed(
            item("<title>Good</title>" + attr("infohash", HEX)),
            item(
                "<title>Conflict</title>"
                + attr("infohash", HEX)
                + attr("magneturl", f"magnet:?xt=urn:btih:{OTHER}&amp;dn=x")
            ),
            item("<title>Good Two</title>" + attr("infohash", OTHER)),
        )
    )
    assert [r.title for r in results] == ["Good", "Good Two"]


def test_bad_optional_date_and_count_keep_the_item():
    results = parse_search_feed(
        feed(
            item(
                "<title>Usable</title>"
                + "<pubDate>not a date at all</pubDate>"
                + attr("seeders", "lots")
                + attr("leechers", "-9")
                + attr("size", "enormous")
                + attr("infohash", HEX)
            )
        )
    )
    assert len(results) == 1
    row = results[0]
    assert row.published_at is None
    assert row.seeders == 0
    assert row.leechers == 0
    assert row.size_bytes is None


# --- RED GROUP 6: bounds / hostile input ----------------------------------

def test_document_byte_bound_rejected():
    with pytest.raises(TorznabParseError):
        parse_search_feed(b"<rss>" + b"a" * (MAX_DOCUMENT_BYTES + 1))


def test_item_count_bound_rejected():
    items = "".join(
        item(f"<title>i{i}</title>" + attr("infohash", HEX))
        for i in range(MAX_ITEMS + 1)
    )
    with pytest.raises(TorznabParseError):
        parse_search_feed(feed(items))


def test_oversized_title_rejects_item():
    oversized = "T" * (MAX_FIELD_LENGTH + 1)
    results = parse_search_feed(feed(item(f"<title>{oversized}</title>" + attr("infohash", HEX))))
    assert results == ()


def test_oversized_guid_is_dropped_but_item_survives():
    oversized = "G" * (MAX_FIELD_LENGTH + 1)
    results = parse_search_feed(
        feed(item(f"<title>Ok</title><guid>{oversized}</guid>" + attr("infohash", HEX)))
    )
    assert len(results) == 1
    assert results[0].guid is None


def test_dtd_bearing_input_rejected():
    raw = (
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "boom">]>'
        b"<rss><channel><item><title>&x;</title></item></channel></rss>"
    )
    with pytest.raises(TorznabParseError):
        parse_search_feed(raw)


def test_entity_bearing_input_rejected():
    raw = (
        b'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "secret">]>'
        b"<rss><channel><item><title>&x;</title></item></channel></rss>"
    )
    with pytest.raises(TorznabParseError):
        parse_search_feed(raw)


def test_attacker_entity_content_never_returns_expanded_title():
    # ElementTree itself expands internal entities, so the pre-parse guard is
    # the only thing standing between an attacker-controlled entity and the
    # output. Assert both the rejection and the non-appearance of the secret.
    secret = "SUPER-SECRET-TOKEN"
    raw = (
        f'<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x "{secret}">]>'
        f"<rss><channel><item><title>&x;</title>"
        f"<torznab:attr xmlns:torznab=\"http://torznab.com/schemas/2015/feed\" "
        f'name="infohash" value="{HEX}"/></item></channel></rss>'
    ).encode()
    with pytest.raises(TorznabParseError):
        parse_search_feed(raw)
    # Re-parse through the raw ElementTree path to document what the guard is
    # protecting against: without it, the entity WOULD be expanded.
    import xml.etree.ElementTree as ET

    expanded = ET.fromstring(raw).findtext(".//title")
    assert expanded == secret


def test_dtd_bearing_utf16_input_rejected():
    # UTF-16 encodes the ASCII declaration tokens with null interleaving, so a
    # byte-level ASCII substring check alone would miss the declaration and
    # ElementTree would expand the entity. The guard must reject it anyway.
    raw = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE rss [<!ENTITY x "boom">]>'
        "<rss><channel><item><title>&x;</title></item></channel></rss>"
    ).encode("utf-16")
    assert b"<!DOCTYPE" not in raw
    assert b"<!ENTITY" not in raw
    with pytest.raises(TorznabParseError):
        parse_search_feed(raw)


def test_dtd_bearing_utf16_be_input_rejected():
    # Big-endian UTF-16 is equally parseable by ElementTree and equally able to
    # expand an internal entity, so the guard must cover it too.
    raw = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE rss [<!ENTITY x "boom">]>'
        "<rss><channel><item><title>&x;</title></item></channel></rss>"
    ).encode("utf-16-be")
    raw = b"\xfe\xff" + raw  # explicit BE BOM
    assert b"<!DOCTYPE" not in raw
    assert b"<!ENTITY" not in raw
    with pytest.raises(TorznabParseError):
        parse_search_feed(raw)


def test_entity_bearing_utf32_input_rejected():
    # UTF-32 is rejected as malformed by ElementTree's expat regardless, so the
    # result is the same TorznabParseError; the guard's utf-32 patterns are
    # defense-in-depth for the same byte-level ASCII-bypass concern.
    raw = (
        '<?xml version="1.0" encoding="UTF-32"?>'
        '<!DOCTYPE rss [<!ENTITY x "secret">]>'
        "<rss><channel><item><title>&x;</title></item></channel></rss>"
    ).encode("utf-32")
    assert b"<!DOCTYPE" not in raw
    assert b"<!ENTITY" not in raw
    with pytest.raises(TorznabParseError):
        parse_search_feed(raw)


def test_attacker_entity_content_never_escapes_via_utf16():
    secret = "SUPER-SECRET-TOKEN"
    raw = (
        f'<?xml version="1.0" encoding="UTF-16"?>'
        f'<!DOCTYPE rss [<!ENTITY x "{secret}">]>'
        "<rss><channel><item><title>&x;</title>"
        f"<torznab:attr xmlns:torznab=\"http://torznab.com/schemas/2015/feed\" "
        f'name="infohash" value="{HEX}"/></item></channel></rss>'
    ).encode("utf-16")
    with pytest.raises(TorznabParseError):
        parse_search_feed(raw)
    # Without the encoding-aware guard, ElementTree would expand the entity and
    # surface the attacker-controlled secret as the title.
    import xml.etree.ElementTree as ET

    expanded = ET.fromstring(raw).findtext(".//title")
    assert expanded == secret
