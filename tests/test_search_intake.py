"""A selected Search result enters Cove through the standard intake path.

Search hands over exactly one magnet URI and stops owning the download: the
existing `MainWindow.add_urls_checked` gate keeps ownership of duplicate
checking, consent, debrid resolution and the torrent fallback. These tests
guard that boundary - the helper must not grow its own queue, debrid or
torrent path, and must not rewrite the magnet a source already normalised.
"""
from PySide6.QtWidgets import QMainWindow

import cove.main_window as mw
from cove.search.models import SearchResult

# Fixture reuse: the real QueueManager environment lives in the queue suite,
# and duplicating it here would create a second queue test framework.
from tests.test_queue import (  # noqa: F401
    _one,
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
#
# A chosen result is a magnet, so past the intake gate it now leaves for the
# interactive metadata preflight rather than committing outright. That route
# -- the resolver, both dialogs, the selection and the commit -- belongs to
# tests/test_search_magnet_contents.py. What these keep proving is that the
# handover into it is the ordinary one: the same gate, the same duplicate
# guard, the same intake label, and no Search-only queue path.


def _torrent_settings():
    return dict(
        torrent_support_enabled=True,
        all_debrid_enabled=True,
        all_debrid_api_key="ad-key-value",
    )


def _preflight_host(queue):
    """A host whose interactive coordinator is recorded instead of shown."""
    host = Host()
    host.queue = queue
    host._items = {}
    host.routed = []
    host._magnet_preflight = lambda url, out_dir, intake: (
        host.routed.append((url, out_dir, intake)), []
    )[1]
    return host


def test_a_selected_result_reaches_the_shared_magnet_coordinator(queue_env):
    """No network, no SearchService, no provider, and nothing durable yet."""
    queue, _rpc, db_path = queue_env(**_torrent_settings())
    host = _preflight_host(queue)

    ids = host.add_search_result(make_result())

    assert host.routed == [(MAGNET, None, "search")]
    assert ids == []
    assert _rows(db_path) == []


def test_search_provenance_survives_the_intake_allowlist(queue_env, diag):
    """The label reaches the diagnostic as "search", not as "unknown".

    The queue normalises any intake it does not recognise, so a Search
    download would otherwise be indistinguishable from a genuinely unknown
    one the moment it crossed the diagnostics boundary. Driven through the
    queue's own commit seam because the GUI coordinator now sits in between.
    """
    queue, _rpc, _db = queue_env(**_torrent_settings())

    tid = queue.add_url(MAGNET, intake="search")

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
    """Search adds no dedupe of its own; the existing info-hash guard runs.

    A live torrent is checked before the coordinator is ever reached, so the
    repeat never becomes a second interactive request either.
    """
    queue, _rpc, db_path = queue_env(**_torrent_settings())
    host = _preflight_host(queue)
    prompts = []
    host._confirm_duplicate = lambda match, label: prompts.append(label) or False
    errors = []
    queue.error.connect(errors.append)

    queue.add_url(f"magnet:?xt=urn:btih:{INFO_HASH}", intake="manual")
    second = host.add_search_result(make_result())

    assert len(_rows(db_path)) == 1
    assert second == []
    assert prompts  # the existing gate asked, before any resolver could start
    assert host.routed == []
