"""The normalised Search data model.

SearchResult is the single shape every source adapter must produce, so it
validates rather than trusts: an adapter that lets a malformed remote row
through gets a ValueError at construction instead of putting a broken magnet
in front of the user.
"""
import pytest

from cove.search.magnet import build_magnet
from cove.search.models import Category, SearchResult, SourceError, SourceErrorKind


HEX = "c9e15763f722f23e98a29decdfae341b98d53056"
OTHER = "0123456789abcdef0123456789abcdef01234567"
MAGNET = build_magnet(HEX, "Example")


def _result(**overrides):
    fields = {
        "info_hash": HEX,
        "name": "Example",
        "magnet": MAGNET,
        "size_bytes": 1024,
        "seeders": 5,
        "leechers": 2,
        "added": 1700000000,
        "source": "yts",
    }
    fields.update(overrides)
    return SearchResult(**fields)


def test_categories_are_the_agreed_set():
    assert {c.name for c in Category} == {"ALL", "GAMES", "MOVIES", "TV", "ANIME"}


def test_valid_result_keeps_its_fields():
    result = _result()
    assert result.info_hash == HEX
    assert result.magnet == MAGNET
    assert result.size_bytes == 1024
    assert result.source == "yts"


def test_result_is_frozen():
    result = _result()
    with pytest.raises(Exception):
        result.name = "changed"


def test_result_allows_unknown_size_and_date():
    result = _result(size_bytes=None, added=None)
    assert result.size_bytes is None
    assert result.added is None


def test_result_rejects_unnormalised_hash():
    with pytest.raises(ValueError):
        _result(info_hash=HEX.upper())


def test_result_rejects_invalid_hash():
    with pytest.raises(ValueError):
        _result(info_hash="deadbeef")


def test_result_rejects_empty_name():
    with pytest.raises(ValueError):
        _result(name="   ")


def test_result_rejects_magnet_for_a_different_hash():
    with pytest.raises(ValueError):
        _result(magnet=build_magnet(OTHER, "Example"))


def test_result_rejects_negative_swarm_counts():
    with pytest.raises(ValueError):
        _result(seeders=-1)
    with pytest.raises(ValueError):
        _result(leechers=-1)


def test_result_rejects_negative_size():
    with pytest.raises(ValueError):
        _result(size_bytes=-1)


def test_result_rejects_empty_source():
    with pytest.raises(ValueError):
        _result(source="")


def test_source_error_carries_a_kind():
    error = SourceError(SourceErrorKind.PARSE, "bad payload")
    assert error.kind is SourceErrorKind.PARSE
    assert "bad payload" in str(error)


def test_source_error_kinds_are_the_agreed_set():
    assert {k.value for k in SourceErrorKind} == {"network", "http", "parse", "timeout"}
