"""Canonical duplicate identity (cove.dedup).

The bias under test is deliberate: only spellings that cannot address
different bytes are collapsed. Everything a real host can use to select a
different file - query string, query order, path case, percent encoding,
trailing slash, non-default port - must stay significant.
"""

import base64

from cove import dedup
from cove.dedup import Candidate

HEX = "0123456789abcdef0123456789abcdef01234567"
B32 = base64.b32encode(bytes.fromhex(HEX)).decode("ascii")


def _same(a: str, b: str) -> bool:
    return dedup.same(Candidate(url=a), Candidate(url=b))


# ---- URL normalisation: what is collapsed ---------------------------


def test_scheme_case_is_insignificant():
    assert _same("HTTPS://example.com/f.zip", "https://example.com/f.zip")


def test_hostname_case_is_insignificant():
    assert _same("https://Example.COM/f.zip", "https://example.com/f.zip")


def test_default_http_port_is_insignificant():
    assert _same("http://example.com:80/f.zip", "http://example.com/f.zip")


def test_default_https_port_is_insignificant():
    assert _same("https://example.com:443/f.zip", "https://example.com/f.zip")


def test_fragment_is_ignored():
    assert _same("https://example.com/f.zip#part2", "https://example.com/f.zip")


# ---- URL normalisation: what stays significant ----------------------


def test_non_default_port_stays_significant():
    assert not _same("https://example.com:8443/f.zip", "https://example.com/f.zip")


def test_query_string_stays_significant():
    assert not _same("https://example.com/f?id=1", "https://example.com/f")


def test_query_order_stays_significant():
    assert not _same("https://example.com/f?a=1&b=2", "https://example.com/f?b=2&a=1")


def test_path_case_stays_significant():
    assert not _same("https://example.com/F.zip", "https://example.com/f.zip")


def test_trailing_slash_stays_significant():
    assert not _same("https://example.com/dir/", "https://example.com/dir")


def test_percent_encoding_stays_significant():
    assert not _same("https://example.com/a%2Fb", "https://example.com/a/b")


def test_differently_signed_urls_do_not_match():
    a = "https://cdn.example.com/f.zip?signature=dummy-signature-a"
    b = "https://cdn.example.com/f.zip?signature=dummy-signature-b"
    assert not _same(a, b)


def test_identical_signed_urls_match():
    url = "https://cdn.example.com/f.zip?token=dummy-token"
    assert _same(url, url)


def test_userinfo_is_preserved():
    assert not _same(
        "https://user:dummy-pass@example.com/f.zip", "https://example.com/f.zip"
    )


def test_malformed_url_does_not_crash():
    for bad in ["", "   ", "http://[::1", "https://example.com:notaport/f", "::::"]:
        assert isinstance(dedup.canonical_url(bad), str)


def test_non_string_url_does_not_crash():
    assert dedup.canonical_url(None) == ""
    assert dedup.canonical_url(42) == ""


def test_empty_candidate_has_no_identity():
    assert dedup.identity(Candidate(url="")) is None


# ---- torrent identity ----------------------------------------------


def test_lowercase_and_uppercase_hex_btih_match():
    assert _same(f"magnet:?xt=urn:btih:{HEX}", f"magnet:?xt=urn:btih:{HEX.upper()}")


def test_base32_btih_matches_equivalent_hex():
    assert _same(f"magnet:?xt=urn:btih:{B32}", f"magnet:?xt=urn:btih:{HEX}")


def test_reordered_magnet_parameters_match():
    a = f"magnet:?xt=urn:btih:{HEX}&dn=Alpha&tr=udp%3A%2F%2Ftracker.a%2Fannounce"
    b = f"magnet:?dn=Alpha&tr=udp%3A%2F%2Ftracker.a%2Fannounce&xt=urn:btih:{HEX}"
    assert _same(a, b)


def test_different_trackers_and_display_names_still_match():
    a = f"magnet:?xt=urn:btih:{HEX}&dn=Alpha&tr=udp%3A%2F%2Ftracker.a%2Fannounce"
    b = f"magnet:?xt=urn:btih:{HEX}&dn=Beta&tr=udp%3A%2F%2Ftracker.b%2Fannounce"
    assert _same(a, b)


def test_private_tracker_passkey_does_not_split_identity():
    a = f"magnet:?xt=urn:btih:{HEX}&tr=https%3A%2F%2Ft.example%2Fdummy-passkey"
    b = f"magnet:?xt=urn:btih:{HEX}"
    assert _same(a, b)


def test_torrent_file_task_hash_matches_magnet_hash():
    from cove import torrent

    file_task = Candidate(
        url=torrent.minimal_magnet(HEX), source_type="torrent", info_hash=HEX
    )
    magnet = Candidate(url=f"magnet:?xt=urn:btih:{HEX}&dn=Alpha")
    assert dedup.same(file_task, magnet)


def test_different_info_hashes_do_not_match():
    other = "89abcdef" + HEX[8:]
    assert not _same(f"magnet:?xt=urn:btih:{HEX}", f"magnet:?xt=urn:btih:{other}")


def test_malformed_btih_yields_no_torrent_identity():
    assert dedup.magnet_info_hash("magnet:?xt=urn:btih:nothex") == ""


def test_magnet_without_btih_yields_no_torrent_identity():
    assert dedup.magnet_info_hash("magnet:?dn=Alpha") == ""


def test_v2_only_magnet_yields_no_torrent_identity():
    v2 = "magnet:?xt=urn:btmh:1220" + "ab" * 32
    assert dedup.magnet_info_hash(v2) == ""


def test_malformed_magnet_falls_back_to_url_identity():
    ident = dedup.identity(Candidate(url="magnet:?xt=urn:btih:nothex"))
    assert ident is not None and ident[0] == dedup.ID_URL


def test_non_magnet_url_has_no_info_hash():
    assert dedup.magnet_info_hash("https://example.com/f.torrent") == ""


def test_normalize_info_hash_tolerates_junk():
    assert dedup.normalize_info_hash("nope") == ""
    assert dedup.normalize_info_hash(None) == ""
    assert dedup.normalize_info_hash(HEX.upper()) == HEX


# ---- identity precedence --------------------------------------------


def test_info_hash_beats_provider_and_url():
    cand = Candidate(
        url="https://example.com/f.zip",
        info_hash=HEX,
        debrid_route="torbox",
        debrid_item_id="42",
    )
    assert dedup.identity(cand) == (dedup.ID_INFO_HASH, HEX)


def test_provider_beats_url_when_both_parts_present():
    cand = Candidate(
        url="https://example.com/f.zip", debrid_route="torbox", debrid_item_id="42"
    )
    assert dedup.identity(cand) == (dedup.ID_PROVIDER, "torbox\x00" + "42")


def test_provider_needs_both_parts():
    cand = Candidate(url="https://example.com/f.zip", debrid_route="torbox")
    ident = dedup.identity(cand)
    assert ident is not None and ident[0] == dedup.ID_URL


def test_provider_identities_are_scoped_by_route():
    a = Candidate(url="https://a.example/x", debrid_route="torbox", debrid_item_id="1")
    b = Candidate(
        url="https://a.example/x", debrid_route="real_debrid", debrid_item_id="1"
    )
    assert not dedup.same(a, b)


# ---- labels are not URLs --------------------------------------------


def test_safe_label_omits_query_and_scheme():
    label = dedup.safe_label(
        Candidate(url="https://cdn.example.com/dir/f.zip?token=dummy-token")
    )
    assert label == "cdn.example.com/f.zip"
    assert "dummy-token" not in label


def test_safe_label_of_magnet_omits_trackers():
    label = dedup.safe_label(
        Candidate(url=f"magnet:?xt=urn:btih:{HEX}&tr=https%3A%2F%2Ft%2Fdummy-passkey")
    )
    assert "dummy-passkey" not in label
    assert "tr=" not in label


def test_safe_label_prefers_an_explicit_name():
    assert dedup.safe_label(Candidate(url="https://x/y", name="Alpha")) == "Alpha"
