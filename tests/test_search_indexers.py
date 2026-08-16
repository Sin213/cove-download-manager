"""Pure model/validation tests for user-configured Torznab indexer records.

Persistence round-trip through the real Settings save/load path lives in
tests/test_config.py; this module covers the record shape, the stable custom
id, validation/dropping, order preservation and secret repr in isolation.
"""
import uuid

import pytest

from cove.search.indexers import (
    MAX_API_KEY_LENGTH,
    MAX_NAME_LENGTH,
    MAX_URL_LENGTH,
    CustomTorznabIndexer,
    new_custom_indexer_id,
    parse_custom_indexers,
)

ID_A = "custom:00000000-0000-0000-0000-000000000001"
ID_B = "custom:00000000-0000-0000-0000-000000000002"

ENDPOINT = "http://127.0.0.1:9696/some/per-indexer/torznab/api"


def _record(**overrides):
    values = {
        "id": ID_A,
        "enabled": True,
        "name": "My indexer",
        "url": ENDPOINT,
        "api_key": "super-secret-test-key",
    }
    values.update(overrides)
    return values


# --- stable id --------------------------------------------------------------


def test_new_id_matches_custom_uuid_format():
    value = new_custom_indexer_id()
    assert value.startswith("custom:")
    suffix = value[len("custom:"):]
    assert str(uuid.UUID(suffix)) == suffix


def test_two_new_ids_are_distinct():
    assert new_custom_indexer_id() != new_custom_indexer_id()


# --- record parsing / validation -------------------------------------------


def test_parse_accepts_a_valid_record():
    (record,) = parse_custom_indexers([_record()])
    assert record.id == ID_A
    assert record.enabled is True
    assert record.name == "My indexer"
    assert record.url == ENDPOINT
    assert record.api_key == "super-secret-test-key"


def test_parse_non_list_returns_empty():
    assert parse_custom_indexers(None) == []
    assert parse_custom_indexers({}) == []
    assert parse_custom_indexers("x") == []


def test_parse_drops_non_object_entries():
    parsed = parse_custom_indexers(["nope", 7, None, _record()])
    assert [r.id for r in parsed] == [ID_A]


@pytest.mark.parametrize(
    "bad_id",
    [
        None,
        "",
        "   ",
        "yts",
        "custom:",
        "custom:not-a-uuid",
        "custom:00000000-0000-0000-0000-00000000000G",
    ],
)
def test_parse_rejects_invalid_or_missing_id(bad_id):
    assert parse_custom_indexers([_record(id=bad_id)]) == []


def test_parse_rejects_non_canonical_uuid_spellings():
    # uuid.UUID accepts braces and uppercase hex, but a persisted id must be
    # exactly "custom:" + the canonical lowercase hyphenated form, so that a
    # hand-edited or non-Cove value can't occupy the custom id space.
    assert parse_custom_indexers(
        [_record(id="custom:{00000000-0000-0000-0000-000000000001}")]
    ) == []
    assert parse_custom_indexers(
        [_record(id="custom:00000000-0000-0000-0000-00000000000A")]
    ) == []


@pytest.mark.parametrize("bad_name", [None, "", "   ", 7, ["x"]])
def test_parse_rejects_empty_or_non_string_name(bad_name):
    assert parse_custom_indexers([_record(name=bad_name)]) == []


def test_parse_trims_name_whitespace():
    (record,) = parse_custom_indexers([_record(name="  My indexer  ")])
    assert record.name == "My indexer"


def test_parse_rejects_overlong_name():
    assert parse_custom_indexers([_record(name="x" * (MAX_NAME_LENGTH + 1))]) == []
    assert parse_custom_indexers([_record(name="x" * MAX_NAME_LENGTH)]) != []


@pytest.mark.parametrize("bad_url", [None, "", "   ", 7])
def test_parse_rejects_empty_or_non_string_url(bad_url):
    assert parse_custom_indexers([_record(url=bad_url)]) == []


def test_parse_trims_url_whitespace():
    (record,) = parse_custom_indexers([_record(url="  " + ENDPOINT + "  ")])
    assert record.url == ENDPOINT


def test_parse_rejects_overlong_url():
    assert parse_custom_indexers([_record(url="http://x/" + "a" * MAX_URL_LENGTH)]) == []


def test_parse_rejects_embedded_control_characters():
    assert parse_custom_indexers([_record(url="http://x/api\x00path")]) == []


def test_parse_roundtrips_api_key_exactly():
    (record,) = parse_custom_indexers([_record(api_key="AbC_123-xyz=+")])
    assert record.api_key == "AbC_123-xyz=+"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_parse_blank_api_key_canonicalizes_to_empty(blank):
    (record,) = parse_custom_indexers([_record(api_key=blank)])
    assert record.api_key == ""


def test_parse_non_string_api_key_canonicalizes_to_empty():
    (record,) = parse_custom_indexers([_record(api_key=42)])
    assert record.api_key == ""


def test_parse_rejects_overlong_api_key():
    assert parse_custom_indexers([_record(api_key="k" * (MAX_API_KEY_LENGTH + 1))]) == []


# --- enabled contract -------------------------------------------------------


def test_parse_defaults_enabled_true_when_missing():
    record = _record()
    del record["enabled"]
    (parsed,) = parse_custom_indexers([record])
    assert parsed.enabled is True


@pytest.mark.parametrize("bogus", ["false", "true", 1, 0, None, [], {}])
def test_parse_non_boolean_enabled_falls_back_to_true(bogus):
    # Matches the Settings bool convention: a non-bool falls back to the
    # field default (True here), and is never read as enabled via truthiness.
    (parsed,) = parse_custom_indexers([_record(enabled=bogus)])
    assert parsed.enabled is True


def test_parse_preserves_explicit_false_enabled():
    (parsed,) = parse_custom_indexers([_record(enabled=False)])
    assert parsed.enabled is False


# --- duplicate identity -----------------------------------------------------


def test_parse_rejects_duplicate_ids_keeping_first():
    parsed = parse_custom_indexers(
        [
            _record(id=ID_A, name="first"),
            _record(id=ID_A, name="dup"),
            _record(id=ID_B, name="second"),
        ]
    )
    assert [r.name for r in parsed] == ["first", "second"]


# --- order ------------------------------------------------------------------


def test_parse_preserves_order():
    parsed = parse_custom_indexers(
        [_record(id=ID_A, name="a"), _record(id=ID_B, name="b")]
    )
    assert [r.id for r in parsed] == [ID_A, ID_B]


# --- secret repr ------------------------------------------------------------


def test_api_key_absent_from_record_repr():
    record = CustomTorznabIndexer(
        id=ID_A, name="n", url=ENDPOINT, api_key="super-secret-test-key"
    )
    assert "super-secret-test-key" not in repr(record)
    assert "api_key" not in repr(record)


def test_blank_api_key_record_repr_still_hides_field_name():
    record = CustomTorznabIndexer(id=ID_A, name="n", url=ENDPOINT)
    assert "api_key" not in repr(record)


# --- edit contract ----------------------------------------------------------


def test_editing_fields_does_not_change_id():
    record = CustomTorznabIndexer(
        id=ID_A, name="old", url="http://old/api", api_key="old-key"
    )
    record.name = "new"
    record.url = "http://new/per-indexer/api"
    record.api_key = "new-key"
    record.enabled = False
    assert record.id == ID_A
