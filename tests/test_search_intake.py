"""A selected Search result enters Cove through the standard intake path.

Search hands over exactly one magnet URI and stops owning the download: the
existing `MainWindow.add_urls_checked` gate keeps ownership of duplicate
checking, consent, debrid resolution and the torrent fallback. These tests
guard that boundary - the helper must not grow its own queue, debrid or
torrent path, and must not rewrite the magnet a source already normalised.
"""
from PySide6.QtWidgets import QMainWindow

import cove.main_window as mw
from cove.queue import SOURCE_TORRENT
from cove.search.models import SearchResult

# Fixture reuse: the real QueueManager environment lives in the queue suite,
# and duplicating it here would create a second queue test framework.
from tests.test_queue import (  # noqa: F401
    _one,
    _persisted_row,
    _rows,
    diag,
    queue_env,
)

INFO_HASH = "0123456789abcdef0123456789abcdef01234567"
# Deliberately busy: a display name and two trackers, so any reconstruction
# of the magnet (rather than a byte-for-byte handover) shows up as a diff.
MAGNET = (
    f"magnet:?xt=urn:btih:{INFO_HASH}&dn=Season+1"
    "&tr=udp%3A%2F%2Ftracker.a.example%3A6969%2Fannounce"
    "&tr=udp%3A%2F%2Ftracker.b.example%3A80%2Fannounce"
)


def make_result(magnet: str = MAGNET) -> SearchResult:
    """A real SearchResult, shaped like what a source actually returns."""
    return SearchResult(
        info_hash=INFO_HASH,
        name="Season 1",
        magnet=magnet,
        size_bytes=1234,
        seeders=12,
        leechers=3,
        added=1_700_000_000,
        source="nyaa",
    )


class Host(mw.MainWindow):
    """The real MainWindow methods, without its heavy constructor."""

    def __init__(self):
        QMainWindow.__init__(self)


def _recording_host():
    """A host that records the exact `add_urls_checked` call it receives."""
    host = Host()
    calls = []

    def spy(urls, out_dir=None, intake="manual"):
        calls.append({"urls": urls, "out_dir": out_dir, "intake": intake})
        return [42]

    host.add_urls_checked = spy
    return host, calls


# ---- Group A: the thin boundary ----------------------------------------


def test_selected_result_is_handed_to_add_urls_checked_as_one_magnet():
    host, calls = _recording_host()
    result = make_result()

    host.add_search_result(result)

    assert len(calls) == 1
    assert calls[0]["urls"] == [result.magnet]
    assert calls[0]["intake"] == "search"


def test_the_url_argument_is_a_one_element_list_not_a_bare_string():
    host, calls = _recording_host()

    host.add_search_result(make_result())

    urls = calls[0]["urls"]
    assert isinstance(urls, list)
    assert len(urls) == 1


def test_the_magnet_crosses_the_boundary_unchanged():
    host, calls = _recording_host()
    result = make_result()

    host.add_search_result(result)

    # Exact string, not merely the same info hash: display name and both
    # trackers must survive, and nothing may be appended.
    assert calls[0]["urls"][0] == MAGNET


def test_no_search_metadata_reaches_the_intake_call():
    host, calls = _recording_host()

    host.add_search_result(make_result())

    call = calls[0]
    assert call["out_dir"] is None
    assert set(call) == {"urls", "out_dir", "intake"}


def test_the_helper_returns_what_add_urls_checked_returns():
    host, calls = _recording_host()

    assert host.add_search_result(make_result()) == [42]


# ---- Group B: the existing magnet path ---------------------------------


def _torrent_settings():
    return dict(
        torrent_support_enabled=True,
        all_debrid_enabled=True,
        all_debrid_api_key="ad-key-value",
    )


def test_a_selected_result_runs_the_standard_magnet_path(queue_env):
    """No network, no SearchService, no provider: a real QueueManager sees
    the magnet arrive through the ordinary torrent classification."""
    queue, _rpc, db_path = queue_env(**_torrent_settings())
    host = Host()
    host.queue = queue
    host._items = {}

    ids = host.add_search_result(make_result())

    assert len(ids) == 1
    row = _persisted_row(db_path, ids[0])
    assert row["source_type"] == SOURCE_TORRENT
    assert row["info_hash"] == INFO_HASH


def test_search_provenance_survives_the_intake_allowlist(queue_env, diag):
    """The label reaches the diagnostic as "search", not as "unknown".

    The queue normalises any intake it does not recognise, so a Search
    download would otherwise be indistinguishable from a genuinely unknown
    one the moment it crossed the diagnostics boundary.
    """
    queue, _rpc, _db = queue_env(**_torrent_settings())
    host = Host()
    host.queue = queue
    host._items = {}

    tid = host.add_search_result(make_result())[0]

    added = _one(diag, "queue", "url_added")
    assert added["task"] == tid
    assert added["fields"]["intake"] == "search"


def test_the_allowlist_still_guards_the_other_intake_values(queue_env, diag):
    """Widening the allowlist by one must not widen it by two."""
    queue, _rpc, _db = queue_env()

    queue.add_url("https://example.invalid/a.zip", intake="manual")
    queue.add_url("https://example.invalid/b.zip", intake="made-up")

    labels = [r["fields"]["intake"] for r in diag.records()
              if r["event"] == "url_added"]
    assert labels == ["manual", "unknown"]


def test_the_standard_duplicate_guard_still_owns_a_repeat_add(queue_env):
    """Search adds no dedupe of its own; the existing info-hash guard runs."""
    queue, _rpc, db_path = queue_env(**_torrent_settings())
    host = Host()
    host.queue = queue
    host._items = {}
    errors = []
    queue.error.connect(errors.append)

    first = host.add_search_result(make_result())
    queue.add_url(f"magnet:?xt=urn:btih:{INFO_HASH}")

    assert len(first) == 1
    assert len(_rows(db_path)) == 1
    assert errors
