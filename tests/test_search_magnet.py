"""Info-hash normalisation and magnet construction/parsing.

Remote indexers hand Cove hashes in whichever shape they please - upper-case
hex, base32, or something malformed - so every one of these helpers has to be
total: a bad value returns None instead of raising into the adapter.
"""
from urllib.parse import quote_plus

import pytest

from cove.search.magnet import (
    TRACKERS,
    build_magnet,
    extract_info_hash,
    normalize_info_hash,
)


HEX = "c9e15763f722f23e98a29decdfae341b98d53056"
BASE32 = "ZHQVOY7XELZD5GFCTXWN7LRUDOMNKMCW"


def test_normalize_passes_through_lowercase_hex():
    assert normalize_info_hash(HEX) == HEX


def test_normalize_lowercases_uppercase_hex():
    assert normalize_info_hash(HEX.upper()) == HEX


def test_normalize_strips_surrounding_whitespace():
    assert normalize_info_hash(f"  {HEX}  ") == HEX


def test_normalize_converts_base32_to_hex():
    assert normalize_info_hash(BASE32) == HEX


def test_normalize_converts_lowercase_base32():
    assert normalize_info_hash(BASE32.lower()) == HEX


def test_normalize_rejects_wrong_length():
    assert normalize_info_hash(HEX[:-1]) is None
    assert normalize_info_hash(HEX + "ab") is None


def test_normalize_rejects_non_hex_characters():
    assert normalize_info_hash("z" * 40) is None


def test_normalize_rejects_undecodable_base32():
    assert normalize_info_hash("1" * 32) is None


def test_normalize_rejects_empty_and_none():
    assert normalize_info_hash("") is None
    assert normalize_info_hash(None) is None


def test_normalize_rejects_all_zero_hash():
    assert normalize_info_hash("0" * 40) is None


def test_extract_reads_hex_btih():
    assert extract_info_hash(f"magnet:?xt=urn:btih:{HEX}&dn=Example") == HEX


def test_extract_reads_base32_btih():
    assert extract_info_hash(f"magnet:?xt=urn:btih:{BASE32}") == HEX


def test_extract_ignores_parameter_order():
    magnet = (
        "magnet:?tr=udp%3A%2F%2Ftracker.example%3A80%2Fannounce"
        f"&dn=Example&xt=urn:btih:{HEX.upper()}"
    )
    assert extract_info_hash(magnet) == HEX


def test_extract_handles_percent_encoded_xt():
    assert extract_info_hash(f"magnet:?xt=urn%3Abtih%3A{HEX}") == HEX


def test_extract_rejects_non_magnet_scheme():
    assert extract_info_hash(f"http://example.invalid/?xt=urn:btih:{HEX}") is None


def test_extract_rejects_missing_xt():
    assert extract_info_hash("magnet:?dn=Example") is None


def test_extract_rejects_other_urn_namespace():
    assert extract_info_hash(f"magnet:?xt=urn:sha1:{BASE32}") is None


def test_extract_rejects_garbage():
    assert extract_info_hash("") is None
    assert extract_info_hash("not a magnet") is None


def test_build_magnet_is_deterministic():
    first = build_magnet(HEX, "Example Name")
    second = build_magnet(HEX, "Example Name")
    assert first == second


def test_build_magnet_uses_default_tracker_list_in_fixed_order():
    magnet = build_magnet(HEX, "Example Name")
    positions = [magnet.index(f"tr={quote_plus(tracker)}") for tracker in TRACKERS]
    assert positions == sorted(positions)
    assert magnet.count("&tr=") == len(TRACKERS)


def test_build_magnet_encodes_the_display_name():
    magnet = build_magnet(HEX, "Some Movie (2020) [1080p]")
    assert "dn=Some+Movie+%282020%29+%5B1080p%5D" in magnet


def test_build_magnet_round_trips_through_extract():
    assert extract_info_hash(build_magnet(HEX, "Example Name")) == HEX


def test_build_magnet_normalises_a_base32_hash():
    assert extract_info_hash(build_magnet(BASE32, "Example")) == HEX


def test_build_magnet_rejects_an_invalid_hash():
    with pytest.raises(ValueError):
        build_magnet("nope", "Example")
