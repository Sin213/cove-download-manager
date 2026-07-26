"""Tests for magnet and .torrent parsing.

Two failure modes drive most of what's here. The first is hashing: an info
hash computed from a re-encoded dictionary silently addresses the wrong
torrent, so the raw-span behaviour is pinned explicitly. The second is path
safety: a .torrent names the files Cove is about to create, so every escape
shape gets its own test.
"""

import hashlib

import pytest

from cove import torrent
from cove.torrent import TorrentError

HASH_HEX = "0123456789abcdef0123456789abcdef01234567"
# Base32 spelling of the same 20 bytes.
HASH_B32 = "AERUKZ4JVPG66AJDIVTYTK6N54ASGRLH"


def benc(value) -> bytes:
    """Minimal bencoder. Dicts keep insertion order so a fixture can be
    deliberately non-canonical."""
    if isinstance(value, bool):
        raise TypeError("bencode has no boolean")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode("utf-8")
    if isinstance(value, bytes):
        return b"%d:" % len(value) + value
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(benc(v) for v in value) + b"e"
    if isinstance(value, dict):
        body = b"".join(benc(k) + benc(v) for k, v in value.items())
        return b"d" + body + b"e"
    raise TypeError(type(value))


def single_file_info(name="movie.mkv", length=1024) -> dict:
    return {b"length": length, b"name": name, b"piece length": 16384, b"pieces": b"\x01" * 20}


def multi_file_info(files=None, name="Season 1") -> dict:
    files = files if files is not None else [
        {b"length": 10, b"path": ["ep1.mkv"]},
        {b"length": 20, b"path": ["extras", "ep2.mkv"]},
    ]
    return {
        b"files": files,
        b"name": name,
        b"piece length": 16384,
        b"pieces": b"\x02" * 40,
    }


def make_torrent(info: dict, extra: dict | None = None) -> bytes:
    root = {b"announce": "http://tracker.example/announce?passkey=SECRETPASS"}
    root.update(extra or {})
    root[b"info"] = info
    return benc(root)


# ---------------------------------------------------------------------------
# Magnets
# ---------------------------------------------------------------------------


def test_hex_btih_is_accepted():
    info = torrent.parse_magnet(f"magnet:?xt=urn:btih:{HASH_HEX}")
    assert info.info_hash == HASH_HEX


def test_base32_btih_is_normalized_to_hex():
    info = torrent.parse_magnet(f"magnet:?xt=urn:btih:{HASH_B32}")
    assert info.info_hash == HASH_HEX


def test_uppercase_hex_is_normalized():
    info = torrent.parse_magnet(f"MAGNET:?XT=URN:BTIH:{HASH_HEX.upper()}&dn=Movie")
    assert info.info_hash == HASH_HEX
    assert info.display_name == "Movie"


@pytest.mark.parametrize("bad", ["", "zz", HASH_HEX[:-1], HASH_HEX + "aa", "1" * 32])
def test_malformed_btih_is_rejected(bad):
    with pytest.raises(TorrentError):
        torrent.parse_magnet(f"magnet:?xt=urn:btih:{bad}")


def test_missing_xt_is_rejected():
    with pytest.raises(TorrentError):
        torrent.parse_magnet("magnet:?dn=Movie&tr=http://tracker.example")


def test_non_magnet_input_is_rejected():
    for bad in ("https://example.com/x", "", None, "magnet:"):
        with pytest.raises(TorrentError):
            torrent.parse_magnet(bad)


def test_contradictory_hashes_are_rejected():
    other = "f" * 40
    with pytest.raises(TorrentError):
        torrent.parse_magnet(f"magnet:?xt=urn:btih:{HASH_HEX}&xt.1=urn:btih:{other}")


def test_repeated_identical_hash_is_accepted():
    uri = f"magnet:?xt=urn:btih:{HASH_HEX}&xt.1=urn:btih:{HASH_HEX.upper()}"
    assert torrent.parse_magnet(uri).info_hash == HASH_HEX


def test_pure_v2_magnet_is_rejected_with_its_own_reason():
    multihash = "1220" + "ab" * 32
    with pytest.raises(TorrentError) as excinfo:
        torrent.parse_magnet(f"magnet:?xt=urn:btmh:{multihash}&dn=Movie")
    assert "v2" in str(excinfo.value)


def test_hybrid_magnet_uses_the_v1_hash():
    multihash = "1220" + "ab" * 32
    uri = f"magnet:?xt=urn:btih:{HASH_HEX}&xt.1=urn:btmh:{multihash}"
    assert torrent.parse_magnet(uri).info_hash == HASH_HEX


def test_over_long_magnet_is_rejected():
    padding = "a" * torrent.MAX_MAGNET_LENGTH
    with pytest.raises(TorrentError):
        torrent.parse_magnet(f"magnet:?xt=urn:btih:{HASH_HEX}&tr={padding}")


def test_minimal_magnet_drops_trackers_and_names():
    original = (
        f"magnet:?xt=urn:btih:{HASH_HEX.upper()}&dn=Some+Movie"
        "&tr=http%3A%2F%2Ftracker.example%2Fannounce%3Fpasskey%3DSECRETPASS"
        "&ws=http://webseed.example/f&xs=http://peer.example"
    )
    info = torrent.parse_magnet(original)
    minimal = torrent.minimal_magnet(info.info_hash)
    assert minimal == f"magnet:?xt=urn:btih:{HASH_HEX}"
    for leaked in ("SECRETPASS", "tracker.example", "webseed", "dn=", "peer.example"):
        assert leaked not in minimal


def test_is_magnet():
    assert torrent.is_magnet(f"magnet:?xt=urn:btih:{HASH_HEX}")
    assert not torrent.is_magnet("https://example.com/a.torrent")
    assert not torrent.is_magnet(None)


# ---------------------------------------------------------------------------
# .torrent structure
# ---------------------------------------------------------------------------


def test_single_file_torrent():
    raw = make_torrent(single_file_info())
    meta = torrent.parse_torrent(raw)
    assert meta.name == "movie.mkv"
    assert meta.multi_file is False
    assert len(meta.files) == 1
    assert meta.files[0].path == ("movie.mkv",)
    assert meta.total_size == 1024
    assert meta.destination_parts(meta.files[0]) == ("movie.mkv",)


def test_multi_file_torrent_keeps_hierarchy():
    meta = torrent.parse_torrent(make_torrent(multi_file_info()))
    assert meta.multi_file is True
    assert [f.path for f in meta.files] == [("ep1.mkv",), ("extras", "ep2.mkv")]
    assert meta.total_size == 30
    assert meta.destination_parts(meta.files[1]) == ("Season 1", "extras", "ep2.mkv")
    assert meta.files[1].relative_path == "extras/ep2.mkv"


def test_hybrid_torrent_with_v1_pieces_is_accepted():
    info = single_file_info()
    info[b"meta version"] = 2
    info[b"file tree"] = {b"movie.mkv": {b"": {b"length": 1024}}}
    meta = torrent.parse_torrent(make_torrent(info))
    assert meta.name == "movie.mkv"


def test_pure_v2_torrent_is_rejected_with_its_own_reason():
    info = {
        b"file tree": {b"movie.mkv": {b"": {b"length": 1024}}},
        b"meta version": 2,
        b"name": "movie.mkv",
        b"piece length": 16384,
    }
    with pytest.raises(TorrentError) as excinfo:
        torrent.parse_torrent(make_torrent(info))
    assert "v2" in str(excinfo.value)


def test_info_hash_is_sha1_of_the_raw_info_span():
    info = single_file_info()
    raw = make_torrent(info)
    encoded_info = benc(info)
    assert raw.count(encoded_info) == 1
    expected = hashlib.sha1(encoded_info).hexdigest()
    assert torrent.parse_torrent(raw).info_hash == expected


def test_non_canonical_info_dict_hashes_its_original_bytes():
    """A re-encoding parser would sort these keys and get a different hash."""
    unsorted_info = {
        b"pieces": b"\x01" * 20,
        b"name": "movie.mkv",
        b"piece length": 16384,
        b"length": 1024,
    }
    raw = make_torrent(unsorted_info)
    original_span = benc(unsorted_info)
    canonical_span = benc(dict(sorted(unsorted_info.items())))
    assert original_span != canonical_span

    parsed = torrent.parse_torrent(raw).info_hash
    assert parsed == hashlib.sha1(original_span).hexdigest()
    assert parsed != hashlib.sha1(canonical_span).hexdigest()


# ---------------------------------------------------------------------------
# Bencode hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    b"",
    b"l1:ae",                       # root is not a dict
    b"not bencode at all",
    b"d",                           # truncated dict
    b"d4:infod1:xi1e",              # truncated nested dict
    b"d3:onei1e",                   # truncated after a value
    b"d3:one4:abce",                # byte string longer than the buffer
])
def test_malformed_bencode_is_rejected(raw):
    with pytest.raises(TorrentError):
        torrent.bdecode_root(raw)


def test_trailing_garbage_is_rejected():
    raw = make_torrent(single_file_info())
    with pytest.raises(TorrentError):
        torrent.parse_torrent(raw + b"junk")


def test_truncated_document_is_rejected():
    raw = make_torrent(single_file_info())
    with pytest.raises(TorrentError):
        torrent.parse_torrent(raw[:-4])


def test_excessive_nesting_is_rejected():
    depth = 200
    raw = b"d4:deep" + b"l" * depth + b"e" * depth + b"e"
    with pytest.raises(TorrentError):
        torrent.bdecode_root(raw)


def test_excessive_list_size_is_rejected():
    body = b"i0e" * (torrent._MAX_ITEMS + 1)
    raw = b"d4:bigl" + body + b"ee"
    with pytest.raises(TorrentError):
        torrent.bdecode_root(raw)


def test_excessive_dict_size_is_rejected():
    body = b"".join(b"%d:k%d" % (len(f"k{i}"), i) + b"i0e" for i in range(torrent._MAX_ITEMS + 1))
    with pytest.raises(TorrentError):
        torrent.bdecode_root(b"d" + body + b"e")


def test_duplicate_dictionary_keys_are_rejected():
    raw = b"d4:infod6:lengthi1ee4:infod6:lengthi2eee"
    with pytest.raises(TorrentError):
        torrent.bdecode_root(raw)


@pytest.mark.parametrize("raw", [
    b"d1:ki e",       # empty integer
    b"d1:ki01ee",     # leading zero
    b"d1:ki-01ee",    # negative with leading zero
    b"d1:ki1ke",      # no terminator
    b"d1:ki+1ee",     # sign not allowed
    b"d1:ki e",
    b"d1:ki" + b"9" * 40 + b"ee",   # absurd digit count
])
def test_invalid_integer_forms_are_rejected(raw):
    with pytest.raises(TorrentError):
        torrent.bdecode_root(raw)


def test_negative_zero_is_rejected():
    with pytest.raises(TorrentError):
        torrent.bdecode_root(b"d1:ki-0ee")


@pytest.mark.parametrize("raw", [
    b"d01:ai0ee",       # leading zero in a length
    b"d1:a99:shorte",   # length beyond the buffer
    b"d-1:ai0ee",       # negative length
    b"di1e1:ae",        # non-string key
])
def test_invalid_byte_lengths_are_rejected(raw):
    with pytest.raises(TorrentError):
        torrent.bdecode_root(raw)


def test_oversized_source_is_rejected_before_parsing():
    with pytest.raises(TorrentError) as excinfo:
        torrent.parse_torrent(b"d" + b"x" * torrent.MAX_TORRENT_BYTES)
    assert "10 MiB" in str(excinfo.value)


def test_missing_info_dictionary_is_rejected():
    with pytest.raises(TorrentError):
        torrent.parse_torrent(benc({b"announce": "http://tracker.example"}))


def test_info_that_is_not_a_dict_is_rejected():
    with pytest.raises(TorrentError):
        torrent.parse_torrent(benc({b"info": [1, 2]}))


def test_missing_name_is_rejected():
    info = single_file_info()
    del info[b"name"]
    with pytest.raises(TorrentError):
        torrent.parse_torrent(make_torrent(info))


def test_empty_name_is_rejected():
    with pytest.raises(TorrentError):
        torrent.parse_torrent(make_torrent(single_file_info(name="   ")))


@pytest.mark.parametrize("length", [-1, "1024", None, 1 << 60])
def test_invalid_file_length_is_rejected(length):
    info = single_file_info()
    if length is None:
        del info[b"length"]
    else:
        info[b"length"] = length
    with pytest.raises(TorrentError):
        torrent.parse_torrent(make_torrent(info))


def test_total_size_overflow_is_rejected():
    chunk = (1 << 50) - 1
    files = [{b"length": chunk, b"path": [f"f{i}.bin"]} for i in range(16)]
    with pytest.raises(TorrentError):
        torrent.parse_torrent(make_torrent(multi_file_info(files)))


def test_duplicate_file_paths_are_rejected():
    files = [
        {b"length": 1, b"path": ["a", "b.bin"]},
        {b"length": 2, b"path": ["a", "b.bin"]},
    ]
    with pytest.raises(TorrentError):
        torrent.parse_torrent(make_torrent(multi_file_info(files)))


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("parts", [
    ["..", "escaped.bin"],
    ["ok", "..", "escaped.bin"],
    ["."],
    ["/etc/passwd"],
    ["/", "etc", "passwd"],
    ["C:", "Windows", "evil.exe"],
    ["C:\\Windows\\evil.exe"],
    ["\\\\server\\share\\evil.exe"],
    ["sub\\dir", "f.bin"],
    ["nul\x00byte.bin"],
    ["CON"],
    ["lpt1.txt"],
    [""],
    ["   "],
    ["..."],
    ["a" * 300],
])
def test_unsafe_paths_are_rejected(parts):
    files = [{b"length": 1, b"path": parts}]
    with pytest.raises(TorrentError):
        torrent.parse_torrent(make_torrent(multi_file_info(files)))


def test_empty_or_missing_path_list_is_rejected():
    for bad in ([], "notalist", None):
        files = [{b"length": 1}] if bad is None else [{b"length": 1, b"path": bad}]
        with pytest.raises(TorrentError):
            torrent.parse_torrent(make_torrent(multi_file_info(files)))


def test_too_many_path_components_are_rejected():
    files = [{b"length": 1, b"path": [f"d{i}" for i in range(torrent._MAX_PATH_PARTS + 1)]}]
    with pytest.raises(TorrentError):
        torrent.parse_torrent(make_torrent(multi_file_info(files)))


def test_unsafe_torrent_name_is_rejected():
    with pytest.raises(TorrentError):
        torrent.parse_torrent(make_torrent(multi_file_info(name="../../etc")))


def test_safe_nested_paths_are_preserved():
    files = [{b"length": 5, b"path": ["Season 1", "Subs", "ep1.en.srt"]}]
    meta = torrent.parse_torrent(make_torrent(multi_file_info(files, name="Show")))
    assert meta.files[0].path == ("Season 1", "Subs", "ep1.en.srt")
    assert meta.files[0].name == "ep1.en.srt"
    assert meta.destination_parts(meta.files[0]) == (
        "Show", "Season 1", "Subs", "ep1.en.srt"
    )


def test_non_utf8_component_falls_back_without_escaping():
    files = [{b"length": 1, b"path": [b"caf\xe9.bin"]}]
    meta = torrent.parse_torrent(make_torrent(multi_file_info(files)))
    component = meta.files[0].path[0]
    assert "/" not in component and "\\" not in component and ".." not in component


def test_safe_component_rejects_non_text():
    for bad in (None, 5, [], b"a\x00b"):
        with pytest.raises(TorrentError):
            torrent.safe_component(bad)


# ---------------------------------------------------------------------------
# Reading from disk
# ---------------------------------------------------------------------------


def test_read_torrent_file_round_trip(tmp_path):
    raw = make_torrent(multi_file_info())
    path = tmp_path / "x.torrent"
    path.write_bytes(raw)
    meta = torrent.read_torrent_file(str(path))
    assert meta.info_hash == torrent.parse_torrent(raw).info_hash
    assert meta.raw_bytes == raw


def test_read_torrent_file_rejects_a_directory(tmp_path):
    with pytest.raises(TorrentError):
        torrent.read_torrent_file(str(tmp_path))


def test_read_torrent_file_rejects_oversized_file(tmp_path):
    path = tmp_path / "big.torrent"
    path.write_bytes(b"d" * (torrent.MAX_TORRENT_BYTES + 1))
    with pytest.raises(TorrentError) as excinfo:
        torrent.read_torrent_file(str(path))
    assert "10 MiB" in str(excinfo.value)


def test_rejection_messages_never_quote_the_input():
    secret = f"magnet:?xt=urn:btih:{'z' * 40}&tr=http://t.example/?passkey=SECRETPASS"
    with pytest.raises(TorrentError) as excinfo:
        torrent.parse_magnet(secret)
    assert "SECRETPASS" not in str(excinfo.value)
    assert "t.example" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# Info-only document (what a debrid provider is allowed to receive)
# ---------------------------------------------------------------------------


def test_info_only_document_drops_tracker_metadata():
    info = multi_file_info()
    raw = make_torrent(info, extra={
        b"announce-list": [["http://tracker.example/announce?passkey=SECRETPASS"]],
        b"url-list": ["http://webseed.example/f"],
        b"comment": "grabbed from private-tracker.example",
        b"created by": "SomeClient/1.0",
    })
    meta = torrent.parse_torrent(raw)
    document = meta.info_only_document()

    for leaked in (b"SECRETPASS", b"announce", b"webseed", b"comment", b"created by"):
        assert leaked not in document
    assert b"passkey" not in document


def test_info_only_document_preserves_the_info_hash():
    raw = make_torrent(multi_file_info(), extra={
        b"announce": "http://tracker.example/announce?passkey=SECRETPASS",
    })
    meta = torrent.parse_torrent(raw)
    document = meta.info_only_document()

    reparsed = torrent.parse_torrent(document)
    assert reparsed.info_hash == meta.info_hash
    assert [f.path for f in reparsed.files] == [f.path for f in meta.files]


def test_info_only_document_reuses_the_original_span_verbatim():
    """Re-encoding would change the bytes, and therefore the hash."""
    unsorted_info = {
        b"pieces": b"\x01" * 20,
        b"name": "movie.mkv",
        b"piece length": 16384,
        b"length": 1024,
    }
    raw = make_torrent(unsorted_info)
    meta = torrent.parse_torrent(raw)
    assert meta.info_bytes == benc(unsorted_info)
    assert torrent.parse_torrent(meta.info_only_document()).info_hash == meta.info_hash


# ---------------------------------------------------------------------------
# Managed .torrent storage (Slice B)
# ---------------------------------------------------------------------------


def _meta(name="movie.mkv"):
    raw = benc({
        b"info": {
            b"length": 7,
            b"name": name,
            b"piece length": 16384,
            b"pieces": b"\x01" * 20,
        }
    })
    return torrent.parse_torrent(raw)


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from cove import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return tmp_path / "torrents"


def test_store_managed_torrent_writes_under_the_data_dir(managed):
    meta = _meta()
    path = torrent.store_managed_torrent(meta)

    assert path == str(managed / f"{meta.info_hash}.torrent")
    assert (managed / f"{meta.info_hash}.torrent").read_bytes() == meta.raw_bytes
    # The original file the user picked is never depended on again.
    assert torrent.read_managed_torrent(path, meta.info_hash) == meta.raw_bytes


def test_store_managed_torrent_uses_owner_only_permissions(managed):
    import os
    import stat

    if os.name != "posix":
        pytest.skip("POSIX mode bits only")
    path = torrent.store_managed_torrent(_meta())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(managed).st_mode) == 0o700


def test_store_managed_torrent_leaves_no_partial_file(managed):
    torrent.store_managed_torrent(_meta())
    assert sorted(p.suffix for p in managed.iterdir()) == [".torrent"]


def test_store_managed_torrent_reuses_an_identical_copy(managed):
    meta = _meta()
    first = torrent.store_managed_torrent(meta)
    before = (managed / f"{meta.info_hash}.torrent").stat().st_mtime_ns
    second = torrent.store_managed_torrent(meta)
    after = (managed / f"{meta.info_hash}.torrent").stat().st_mtime_ns

    assert first == second
    assert before == after


def test_store_managed_torrent_replaces_junk_under_its_own_name(managed):
    meta = _meta()
    managed.mkdir(parents=True, exist_ok=True)
    target = managed / f"{meta.info_hash}.torrent"
    target.write_bytes(b"not a torrent")

    path = torrent.store_managed_torrent(meta)
    assert torrent.read_managed_torrent(path, meta.info_hash) == meta.raw_bytes


def test_store_managed_torrent_refuses_a_symlinked_target(managed, tmp_path):
    import os

    if not hasattr(os, "symlink"):
        pytest.skip("no symlinks here")
    meta = _meta()
    managed.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "victim.bin"
    outside.write_bytes(b"important")
    os.symlink(outside, managed / f"{meta.info_hash}.torrent")

    with pytest.raises(TorrentError):
        torrent.store_managed_torrent(meta)
    # The attacker-controlled target is untouched.
    assert outside.read_bytes() == b"important"


def test_read_managed_torrent_rejects_a_missing_copy(managed):
    meta = _meta()
    with pytest.raises(TorrentError):
        torrent.read_managed_torrent(
            str(managed / f"{meta.info_hash}.torrent"), meta.info_hash
        )


def test_read_managed_torrent_rejects_a_replaced_torrent(managed):
    meta = _meta()
    other = _meta("other.mkv")
    path = torrent.store_managed_torrent(meta)
    open(path, "wb").write(other.raw_bytes)

    with pytest.raises(TorrentError):
        torrent.read_managed_torrent(path, meta.info_hash)


def test_read_managed_torrent_rejects_a_corrupted_copy(managed):
    meta = _meta()
    path = torrent.store_managed_torrent(meta)
    open(path, "wb").write(b"garbage")

    with pytest.raises(TorrentError) as exc:
        torrent.read_managed_torrent(path, meta.info_hash)
    assert "garbage" not in str(exc.value)


def test_managed_torrent_errors_never_echo_the_bytes(managed):
    meta = _meta()
    path = torrent.store_managed_torrent(meta)
    open(path, "wb").write(b"d4:infod6:secretsSECRETPASSee")

    with pytest.raises(TorrentError) as exc:
        torrent.read_managed_torrent(path, meta.info_hash)
    assert "SECRETPASS" not in str(exc.value)


def test_discard_managed_torrent_only_deletes_inside_the_managed_dir(managed, tmp_path):
    meta = _meta()
    path = torrent.store_managed_torrent(meta)
    torrent.discard_managed_torrent(path)
    assert not (managed / f"{meta.info_hash}.torrent").exists()

    outside = tmp_path / "user.torrent"
    outside.write_bytes(meta.raw_bytes)
    torrent.discard_managed_torrent(str(outside))
    assert outside.exists()
    # Missing paths and junk are no-ops rather than errors.
    torrent.discard_managed_torrent(path)
    torrent.discard_managed_torrent("")
    torrent.discard_managed_torrent(None)


def test_is_managed_torrent_path(managed, tmp_path):
    meta = _meta()
    path = torrent.store_managed_torrent(meta)
    assert torrent.is_managed_torrent_path(path) is True
    assert torrent.is_managed_torrent_path(str(tmp_path / "x.torrent")) is False
    assert torrent.is_managed_torrent_path("") is False
