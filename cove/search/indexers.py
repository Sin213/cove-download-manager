"""Persisted configuration for user-configured generic Torznab indexers.

These records are data only. S2 persists them so a later slice can build a
network-backed Torznab source from each one; nothing here performs network
I/O, touches SearchService/registry, or draws UI.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

# The "custom:" prefix keeps user records apart from the fixed built-in source
# ids (yts, nyaa, ...) that cove.search.sources registers at import time.
CUSTOM_ID_PREFIX = "custom:"

# Conservative structural bounds. These bound persisted values only; they do
# not model any network/security policy, which belongs to later slices.
MAX_NAME_LENGTH = 200
MAX_URL_LENGTH = 2048
MAX_API_KEY_LENGTH = 512


def new_custom_indexer_id() -> str:
    """A fresh stable source id, generated exactly once per new record."""
    return f"{CUSTOM_ID_PREFIX}{uuid.uuid4()}"


def _is_valid_custom_id(value) -> bool:
    """Whether `value` is exactly ``custom:`` + a canonical lowercase UUID."""
    if not isinstance(value, str):
        return False
    if not value.startswith(CUSTOM_ID_PREFIX):
        return False
    suffix = value[len(CUSTOM_ID_PREFIX):]
    try:
        parsed = uuid.UUID(suffix)
    except (ValueError, AttributeError):
        return False
    # uuid.UUID accepts braces, uppercase hex and loose spellings; a persisted
    # id must be the exact canonical form so a hand-edited value cannot occupy
    # the custom id space, and the bound stays small and deterministic.
    return str(parsed) == suffix


@dataclass
class CustomTorznabIndexer:
    """One user-configured Torznab indexer, as persisted in Settings.

    ``id`` is a stable logical source identity (``custom:<uuid>``), never
    derived from the editable fields, and never regenerated on load or save.
    ``url`` is the full configured Torznab endpoint, preserved exactly as
    entered (trimmed), with no path rewriting or query construction. ``api_key``
    is an optional secret kept at rest by Cove's existing settings-file policy
    and hidden from the normal record repr.
    """

    id: str
    enabled: bool = True
    name: str = ""
    url: str = ""
    api_key: str = field(default="", repr=False)


def _clean_required_str(value, limit: int) -> str | None:
    """The trimmed, bounded, control-free string `value`, or None if invalid."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > limit:
        return None
    if any(ord(c) < 0x20 for c in cleaned):
        return None
    return cleaned


def _parse_one(entry) -> CustomTorznabIndexer | None:
    """One valid record from a persisted entry, or None to drop it."""
    if not isinstance(entry, dict):
        return None
    raw_id = entry.get("id")
    if not _is_valid_custom_id(raw_id):
        return None
    name = _clean_required_str(entry.get("name"), MAX_NAME_LENGTH)
    if name is None:
        return None
    url = _clean_required_str(entry.get("url"), MAX_URL_LENGTH)
    if url is None:
        return None
    api_key = entry.get("api_key", "")
    if not isinstance(api_key, str):
        api_key = ""
    elif not api_key.strip():
        # Blank (empty or whitespace-only) canonicalizes to the no-secret form.
        api_key = ""
    if len(api_key) > MAX_API_KEY_LENGTH:
        return None
    enabled = entry.get("enabled", True)
    # Matches the Settings bool convention: a non-bool falls back to the field
    # default rather than being read as enabled via Python truthiness.
    if not isinstance(enabled, bool):
        enabled = True
    return CustomTorznabIndexer(
        id=raw_id,
        enabled=enabled,
        name=name,
        url=url,
        api_key=api_key,
    )


def parse_custom_indexers(raw) -> list[CustomTorznabIndexer]:
    """The valid custom indexers in a persisted payload, in stored order.

    A record is dropped when it is not an object, has an invalid or duplicate
    id, an empty/overlong name or url, or an overlong api_key. Duplicate ids
    are never accepted as distinct sources: only the first occurrence of an id
    is kept. Identity is never regenerated here - a bad id drops the record,
    it does not mint a new one.
    """
    if not isinstance(raw, list):
        return []
    records: list[CustomTorznabIndexer] = []
    seen: set[str] = set()
    for entry in raw:
        record = _parse_one(entry)
        if record is None:
            continue
        if record.id in seen:
            continue
        seen.add(record.id)
        records.append(record)
    return records
