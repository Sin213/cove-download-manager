"""CDM-003: authorization must precede the peer-capable metadata RPC.

Reuse the manual/Search coordinator harness and metadata-only RPC spy. All
network operations in this module are fakes; no real swarm is contacted.
"""
from collections import deque
from pathlib import Path

import pytest

import cove.main_window as mw
import cove.queue as qm
from tests.test_queue import queue_env, _rows  # noqa: F401
from tests.test_magnet_metadata import _fixture_bytes, _magnet, _other_bytes
from tests.test_manual_magnet_contents import (
    _REJECT, _env, _errors, _fixture_cached, _inline, _plan, _running,
)
from tests.test_search_magnet_contents import _result


FALLBACK_BLOCKED = (
    "Cove cannot fetch this torrent's file list because Settings → BitTorrent → "
    "“When a torrent is not cached” is set to “Cancel the download”, which "
    "also blocks local BitTorrent for the file list."
)
PROXY_BLOCKED = (
    "Cove cannot fetch this torrent's file list because a proxy is enabled. "
    "To allow local BitTorrent anyway, tick “Allow local BitTorrent while "
    "proxy settings are enabled” in Settings → BitTorrent."
)
CONSENT_REQUIRED = (
    "Cove cannot fetch this torrent's file list without permission to use "
    "local BitTorrent."
)
CONSENT_DECLINED = (
    "Fetching this torrent's file list through local BitTorrent was declined."
)
BLOCKS = [
    pytest.param({"torrent_fallback_mode": "never"}, FALLBACK_BLOCKED, id="fallback"),
    pytest.param(
        {"proxy_type": "http", "proxy_host": "127.0.0.1",
         "torrent_allow_with_proxy": False},
        PROXY_BLOCKED, id="proxy",
    ),
]


@pytest.mark.parametrize("actual,expected,labels", [
    pytest.param(qm.MAGNET_P2P_FALLBACK_BLOCKED, FALLBACK_BLOCKED,
                 ("When a torrent is not cached", "Cancel the download"),
                 id="fallback"),
    pytest.param(qm.MAGNET_P2P_PROXY_BLOCKED, PROXY_BLOCKED,
                 ("Allow local BitTorrent while proxy settings are enabled",),
                 id="proxy"),
])
def test_blocked_sentences_use_settings_labels(actual, expected, labels):
    assert actual == expected
    assert all(label in actual for label in labels)
    assert "fallback preference" not in actual and "around it" not in actual


@pytest.fixture
def notices(monkeypatch):
    """Record the existing information/warning surface without a modal."""
    calls = []

    def show(parent, title, text, *args, **kwargs):
        calls.append((title, text))
        return mw.QMessageBox.Ok

    monkeypatch.setattr(mw.QMessageBox, "information", show)
    monkeypatch.setattr(mw.QMessageBox, "warning", show)
    return calls


class _Consent:
    """Drive the real, patchable four-tuple builder; never fork its UI."""

    def __init__(self, monkeypatch, choices=("accept",), remembers=(), hooks=()):
        real_build = mw.build_p2p_consent_box
        self.choices = deque(choices)
        self.remembers = deque(remembers)
        self.hooks = deque(hooks)
        self.calls = []
        self.events = []
        self.depth = 0
        self.max_depth = 0

        def build(parent, **kwargs):
            box, accept, settings, remember = real_build(parent, **kwargs)
            choice = self.choices.popleft() if self.choices else "cancel"
            remember.setChecked(self.remembers.popleft() if self.remembers else False)
            cancel = next(b for b in box.buttons()
                          if box.buttonRole(b) == mw.QMessageBox.RejectRole)
            clicked = {"accept": accept, "settings": settings,
                       "cancel": cancel, "dismiss": None}[choice]
            self.calls.append((box, accept, settings, remember))

            def execute():
                self.depth += 1
                self.max_depth = max(self.max_depth, self.depth)
                try:
                    self.events.append(choice)
                    if self.hooks:
                        self.hooks.popleft()()
                finally:
                    self.depth -= 1
                return 0

            box.exec = execute
            box.clickedButton = lambda: clicked
            return box, accept, settings, remember

        monkeypatch.setattr(mw, "build_p2p_consent_box", build)


def _workspaces(monkeypatch, tmp_path, after_create=None):
    original = qm.tempfile.mkdtemp
    created = []

    def create(*args, **kwargs):
        kwargs.setdefault("dir", str(tmp_path))
        path = original(*args, **kwargs)
        created.append(Path(path))
        if after_create is not None:
            after_create()
        return path

    monkeypatch.setattr(qm.tempfile, "mkdtemp", create)
    return created


def _empty(queue, rpc, db_path):
    assert _rows(db_path) == []
    assert queue.tasks == {}
    assert rpc.magnets == [] and rpc.torrents == []
    assert queue._preflight_hashes == set()
    assert queue._magnet_resolutions == {}
    assert queue._consent_granted == set()


def _admit(host, meta, route):
    if route == "manual":
        return host.add_urls_checked([_magnet(meta.info_hash)])
    if route == "forwarded":
        return host.add_url_interactive(_magnet(meta.info_hash))
    return host.add_search_result(_result(meta))


@pytest.mark.parametrize("route", ["manual", "search", "forwarded"])
@pytest.mark.parametrize("settings,message", BLOCKS)
def test_p1_p2_p7_hard_blocks_before_metadata(
    queue_env, monkeypatch, tmp_path, notices, route, settings, message,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, **settings,
        torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch)
    created = _workspaces(monkeypatch, tmp_path)
    errors = _errors(queue)
    meta = _plan(rpc)

    _admit(host, meta, route)

    assert len(rpc.metadata_added) == 0, f"metadata admissions: expected 0, observed {len(rpc.metadata_added)}"
    assert created == []
    assert consent.calls == []  # consent cannot override a hard policy
    assert loading.calls == [] and contents.calls == [] and not pending
    _empty(queue, rpc, db_path)
    assert errors == [message]
    assert len(notices) == 1 and notices[0][1] == message


@pytest.mark.parametrize("route", ["manual", "search", "forwarded"])
@pytest.mark.parametrize("choice", ["cancel", "dismiss"])
def test_p3_p7_decline_is_silent_and_has_no_side_effects(
    queue_env, monkeypatch, tmp_path, notices, route, choice,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch, [choice], [True])
    created = _workspaces(monkeypatch, tmp_path)
    saves = []
    monkeypatch.setattr(queue.settings, "save", lambda: saves.append(True))
    errors = _errors(queue)
    meta = _plan(rpc)

    _admit(host, meta, route)

    assert len(rpc.metadata_added) == 0, f"metadata admissions: expected 0, observed {len(rpc.metadata_added)}"
    assert len(consent.calls) == 1
    assert created == [] and not pending
    assert loading.calls == [] and contents.calls == []
    assert not queue.settings.torrent_ip_disclosure_shown and saves == []
    assert errors == [CONSENT_DECLINED] and notices == []
    _empty(queue, rpc, db_path)


@pytest.mark.parametrize("autostart", [False, True], ids=["P4", "P13"])
def test_p4_p13_request_grant_reaches_payload_before_it_can_start(
    queue_env, monkeypatch, tmp_path, notices, autostart,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch)
    payload_prompts = []
    queue.torrent_consent_needed.connect(payload_prompts.append)
    meta = _plan(rpc)

    def start_inline(_dlg=None):
        _inline(queue)
        _running(queue)

    if autostart:
        contents.during_exec = start_inline
    ids = host.add_urls_checked([_magnet(meta.info_hash)])
    if not autostart:
        start_inline()
        queue._maybe_start_next()

    assert payload_prompts == [], f"payload consent prompts: expected [], observed {payload_prompts}"
    assert len(consent.calls) == 1
    assert len(rpc.metadata_added) == 1 and len(contents.calls) == 1
    assert len(ids) == 1 and len(_rows(db_path)) == 1
    assert len(rpc.torrents) == 1 and rpc.magnets == []
    assert ids[0] in queue._consent_granted
    assert not queue.settings.torrent_ip_disclosure_shown


def test_p5_remember_is_saved_before_metadata_and_covers_payload(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch, remembers=[True])
    events = []
    monkeypatch.setattr(queue.settings, "save", lambda: events.append(
        ("save", queue.settings.torrent_ip_disclosure_shown)))
    original = rpc.add_magnet_metadata

    def admit(*args):
        events.append(("rpc", tuple(consent.events)))
        return original(*args)

    monkeypatch.setattr(rpc, "add_magnet_metadata", admit)
    payload_prompts = []
    queue.torrent_consent_needed.connect(payload_prompts.append)
    contents.during_exec = lambda _dlg: (_inline(queue), _running(queue))
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert events == [("save", True), ("rpc", ("accept",))], f"expected save then authorized RPC; observed {events}"
    assert queue.settings.torrent_ip_disclosure_shown
    assert payload_prompts == [] and len(rpc.torrents) == 1
    assert len(_rows(db_path)) == 1 and len(consent.calls) == 1


@pytest.mark.parametrize("decline_first", [False, True], ids=["P6", "P11"])
def test_p6_p11_fifo_each_request_gets_its_own_decision(
    queue_env, monkeypatch, tmp_path, notices, decline_first,
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    first = _plan(rpc)
    second = _plan(rpc, _other_bytes())
    enqueued = []

    def enqueue_second():
        if not enqueued:
            enqueued.append(host.add_urls_checked([_magnet(second.info_hash)]))

    consent = _Consent(
        monkeypatch, ["cancel" if decline_first else "accept", "accept"],
        [decline_first, False], [enqueue_second],
    )
    # Also drive the existing seam on the baseline, where no disclosure runs.
    # This makes its RED expose two admissions rather than an absent new API.
    loading.hooks = deque([lambda dlg: (enqueue_second(), deliver(dlg))])
    errors = _errors(queue)

    host.add_urls_checked([_magnet(first.info_hash)])

    expected = [second.info_hash] if decline_first else [first.info_hash, second.info_hash]
    assert [call["info_hash"] for call in rpc.metadata_added] == expected, "expected only B admitted after A declined; observed A and B"
    assert len(consent.calls) == 2, f"metadata disclosures: expected 2, observed {len(consent.calls)}"
    assert enqueued == [[]] and consent.max_depth == 1 and loading.max_open == 1
    assert len(_rows(db_path)) == len(expected) and len(contents.calls) == len(expected)
    assert not queue.settings.torrent_ip_disclosure_shown
    assert queue._preflight_hashes == set() and queue._magnet_resolutions == {}
    assert not pending and not host._magnet_requests and not host._magnet_loading
    assert errors == ([CONSENT_DECLINED] if decline_first else [])


def test_p7_manual_and_search_use_the_same_coordinator(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT,
    )
    first = _plan(rpc)
    second = _plan(rpc, _other_bytes())
    calls = []
    original = host._resolve_magnet_request

    def resolve(url, out_dir, intake):
        calls.append((url, intake))
        return original(url, out_dir, intake)

    monkeypatch.setattr(host, "_resolve_magnet_request", resolve)
    _admit(host, first, "manual")
    _admit(host, second, "search")

    assert [intake for url, intake in calls] == ["manual", "search"]
    assert len(rpc.metadata_added) == 2 and len(contents.calls) == 2
    _empty(queue, rpc, db_path)


@pytest.mark.parametrize("route", ["manual", "search", "forwarded"])
def test_p7_authorized_request_reaches_contents_after_disclosure(
    queue_env, monkeypatch, tmp_path, notices, route,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT,
        torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch)
    at_admission = []
    original = rpc.add_magnet_metadata

    def admit(*args):
        at_admission.append(tuple(consent.events))
        return original(*args)

    monkeypatch.setattr(rpc, "add_magnet_metadata", admit)
    meta = _plan(rpc)

    _admit(host, meta, route)

    assert at_admission == [("accept",)], "expected accepted disclosure before RPC; observed RPC before disclosure"
    assert len(rpc.metadata_added) == 1 and len(contents.calls) == 1
    assert len(consent.calls) == 1 and not pending
    assert not queue.settings.torrent_ip_disclosure_shown
    _empty(queue, rpc, db_path)  # declining Contents drops the request grant


@pytest.mark.parametrize("settings,message", BLOCKS)
@pytest.mark.parametrize("flip_at", ["worker", "workspace"])
def test_p8_worker_rechecks_current_policy_at_rpc_admission(
    queue_env, monkeypatch, tmp_path, notices, settings, message, flip_at,
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT,
        torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch)
    handles = []
    original = queue.resolve_magnet_preflight

    def resolve(*args, **kwargs):
        handle = original(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(queue, "resolve_magnet_preflight", resolve)

    def stricter():
        for name, value in settings.items():
            setattr(queue.settings, name, value)

    created = _workspaces(
        monkeypatch, tmp_path, stricter if flip_at == "workspace" else None,
    )
    loading.hooks = deque([lambda dlg: (
        stricter() if flip_at == "worker" else None, deliver(dlg))])
    errors = _errors(queue)
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(rpc.metadata_added) == 0, f"metadata admissions after policy change: expected 0, observed {len(rpc.metadata_added)}"
    assert len(handles) == 1 and handles[0].state == "blocked"
    assert errors == [message] and len(notices) == 1 and notices[0][1] == message
    assert len(consent.calls) == 1 and contents.calls == []
    assert all(not path.exists() for path in created)
    assert loading.calls[0].finished and not pending
    _empty(queue, rpc, db_path)


@pytest.mark.parametrize("settings,message", BLOCKS + [
    pytest.param({}, CONSENT_REQUIRED, id="no-consent"),
])
def test_p9_queue_refuses_without_a_grant_independently_of_ui(
    queue_env, monkeypatch, tmp_path, notices, settings, message,
):
    _host, queue, rpc, db_path, _c, _l, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, **settings,
        torrent_ip_disclosure_shown=False,
    )
    _inline(queue)
    created = _workspaces(monkeypatch, tmp_path)
    errors = _errors(queue)
    resolved, failed = [], []
    meta = _plan(rpc)
    uri = _magnet(meta.info_hash, dn="PRIVATE_NAME", trackers=(
        "https://tracker.invalid/announce?passkey=PRIVATE_KEY",))

    handle = queue.resolve_magnet_preflight(
        uri, on_resolved=resolved.append, on_failed=failed.append,
    )

    assert len(rpc.metadata_added) == 0, f"unconsented queue metadata admissions: expected 0, observed {len(rpc.metadata_added)}"
    assert handle.state == "blocked" and failed == [handle] and resolved == []
    assert errors == [message] and created == [] and not pending
    assert all(secret not in errors[0] for secret in (
        "PRIVATE_NAME", "PRIVATE_KEY", "tracker.invalid", "magnet:", meta.info_hash))
    _empty(queue, rpc, db_path)


def test_p10_local_file_list_needs_no_metadata_disclosure(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch)
    source = tmp_path / "picked.torrent"
    source.write_bytes(_fixture_bytes())

    queue.add_torrent_file(str(source), str(tmp_path), precommit=host._torrent_preflight)
    deliver()

    assert consent.calls == [] and rpc.metadata_added == [] and loading.calls == []
    assert len(contents.calls) == 1 and len(_rows(db_path)) == 1
    assert not queue.settings.torrent_ip_disclosure_shown
    payload_prompts = []
    queue.torrent_consent_needed.connect(payload_prompts.append)
    _inline(queue)
    _running(queue)
    queue._maybe_start_next()
    assert payload_prompts == list(queue.tasks)
    assert rpc.torrents == [] and rpc.magnets == []


def test_p12_cancel_after_admission_cleans_up_and_never_commits(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT,
    )
    # Persistent authorization keeps this an existing cancellation regression,
    # rather than manufacturing RED merely because the new UI is absent.
    _Consent(monkeypatch)
    created = _workspaces(monkeypatch, tmp_path)
    meta = _plan(rpc, on_complete=lambda: host._magnet_loading[0].cancel())
    errors = _errors(queue)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(rpc.metadata_added) == 1
    gid = rpc.metadata_added[0]["gid"]
    assert rpc.removed == [gid] and rpc.results_removed == [gid]
    assert all(not path.exists() for path in created)
    assert contents.calls == [] and loading.calls[0].finished
    assert errors == [] and not pending
    _empty(queue, rpc, db_path)


@pytest.mark.parametrize("settings,message", BLOCKS)
@pytest.mark.parametrize("grant", ["persistent", "request"])
def test_p14_hard_policy_outranks_every_kind_of_consent(
    queue_env, monkeypatch, tmp_path, notices, settings, message, grant,
):
    _host, queue, rpc, db_path, _c, _l, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, **settings,
        torrent_ip_disclosure_shown=(grant == "persistent"),
    )
    _inline(queue)
    created = _workspaces(monkeypatch, tmp_path)
    errors = _errors(queue)
    resolved, failed = [], []
    meta = _plan(rpc)
    kwargs = {"p2p_consent_granted": True} if grant == "request" else {}

    handle = queue.resolve_magnet_preflight(
        _magnet(meta.info_hash), on_resolved=resolved.append,
        on_failed=failed.append, **kwargs,
    )

    assert len(rpc.metadata_added) == 0, f"metadata admissions despite hard policy and consent: expected 0, observed {len(rpc.metadata_added)}"
    assert handle.state == "blocked" and failed == [handle] and resolved == []
    assert errors == [message] and created == []
    _empty(queue, rpc, db_path)


def test_metadata_disclosure_wording_buttons_and_order_are_truthful(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT,
        torrent_ip_disclosure_shown=False,
    )
    before = []
    records = []
    original_record = mw._MagnetRequest

    def create_record(owner):
        record = original_record(owner)
        records.append(record)
        return record

    monkeypatch.setattr(mw, "_MagnetRequest", create_record)
    consent = _Consent(monkeypatch, hooks=[lambda: before.append((
        len(loading.calls), len(host._magnet_loading), len(rpc.metadata_added),
        len(pending), len(queue._magnet_resolutions), len(records)))])
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert before == [(0, 0, 0, 0, 0, 0)], "expected disclosure before loading, request creation, worker and RPC; observed no disclosure"
    assert len(consent.calls) == 1
    box, accept, settings, remember = consent.calls[0]
    text = box.text().lower()
    assert "metadata" in text and "file list" in text
    assert all(word in text for word in ("trackers", "dht", "peers", "ip address"))
    assert "before" in text and "download" in text
    assert "not available through any enabled debrid service" not in text
    for claim in ("anonymous", "anonymity", "kill switch", "fully protected",
                  "you are protected", "encrypted end-to-end"):
        assert claim not in text
    assert "file list" in accept.text().lower() and "download locally" not in accept.text().lower()
    assert settings.text() == "Open Settings"
    assert box.defaultButton() is box.escapeButton()
    assert box.buttonRole(box.defaultButton()) == mw.QMessageBox.RejectRole
    assert "cancel" in box.defaultButton().text().lower()
    default, download, open_settings, default_remember = mw.build_p2p_consent_box(host)
    assert default_remember.text() == "Don't show this notice again"
    assert default.windowTitle() == "Torrent is not cached"
    assert default.text() == (
        "This torrent is not available through any enabled debrid service. Cove "
        "is configured to download uncached torrents directly through "
        "BitTorrent.\n\n"
        "Local BitTorrent exposes your IP address to peers and trackers. You "
        "can bind Cove to a VPN network interface or cancel uncached torrents "
        "under Settings → BitTorrent.\n\n"
        "Cove cannot verify that your VPN is active."
    )
    assert download.text() == "Download locally" and open_settings.text() == "Open Settings"
    assert default.defaultButton().text() == "Cancel download"
    assert default.escapeButton() is default.defaultButton() and len(default.buttons()) == 3
    assert remember.text() == "Don't ask again before Cove uses local BitTorrent"
    _empty(queue, rpc, db_path)


def test_remember_save_failure_reverts_flag_but_preserves_request_grant(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch, remembers=[True])
    attempts = []

    def fail_save():
        attempts.append(queue.settings.torrent_ip_disclosure_shown)
        raise OSError("private filesystem detail")

    monkeypatch.setattr(queue.settings, "save", fail_save)
    prompts = []
    queue.torrent_consent_needed.connect(prompts.append)
    contents.during_exec = lambda _dlg: (_inline(queue), _running(queue))
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert attempts == [True], f"remember save attempts: expected [True], observed {attempts}"
    assert not queue.settings.torrent_ip_disclosure_shown
    assert len(rpc.metadata_added) == 1 and len(rpc.torrents) == 1
    assert prompts == [] and len(consent.calls) == 1
    assert len(notices) == 1 and "could not" in notices[0][1].lower()
    assert "sav" in notices[0][1].lower() and "private filesystem detail" not in notices[0][1]


def test_open_settings_discards_remember_and_reevaluates_hard_policy(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch, ["settings"], [True])
    settings_opened, saves = [], []

    def open_settings():
        settings_opened.append(True)
        queue.settings.torrent_fallback_mode = "never"

    monkeypatch.setattr(host, "_open_settings", open_settings)
    monkeypatch.setattr(queue.settings, "save", lambda: saves.append(True))
    errors = _errors(queue)
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(rpc.metadata_added) == 0, f"metadata admissions after Settings forbids P2P: expected 0, observed {len(rpc.metadata_added)}"
    assert settings_opened == [True] and len(consent.calls) == 1
    assert not queue.settings.torrent_ip_disclosure_shown and saves == []
    assert errors == [FALLBACK_BLOCKED] and len(notices) == 1
    assert loading.calls == [] and contents.calls == [] and not pending
    _empty(queue, rpc, db_path)


def test_shutdown_during_disclosure_cannot_start_metadata(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch, hooks=[lambda: setattr(host, "_preflights_closed", True)])
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(rpc.metadata_added) == 0, f"metadata admissions after disclosure closes window: expected 0, observed {len(rpc.metadata_added)}"
    assert len(consent.calls) == 1
    assert loading.calls == [] and contents.calls == [] and not pending
    _empty(queue, rpc, db_path)


def test_open_settings_reasks_without_inheriting_checkbox(
    queue_env, monkeypatch, tmp_path, notices,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, decide=_REJECT,
        torrent_ip_disclosure_shown=False,
    )
    consent = _Consent(monkeypatch, ["settings", "accept"], [True, False])
    settings_opened, saves = [], []
    monkeypatch.setattr(host, "_open_settings", lambda: settings_opened.append(True))
    monkeypatch.setattr(queue.settings, "save", lambda: saves.append(True))
    meta = _plan(rpc)

    host.add_urls_checked([_magnet(meta.info_hash)])

    assert len(consent.calls) == 2, f"metadata disclosures around Settings: expected 2, observed {len(consent.calls)}"
    assert settings_opened == [True] and saves == []
    assert not queue.settings.torrent_ip_disclosure_shown
    assert len(rpc.metadata_added) == 1 and len(contents.calls) == 1
    _empty(queue, rpc, db_path)


def test_torrent_disabled_queue_protection_already_precedes_metadata(
    queue_env, monkeypatch, tmp_path, notices,
):
    _host, queue, rpc, db_path, _c, _l, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_support_enabled=False,
        torrent_ip_disclosure_shown=True, torrent_fallback_mode="never",
        proxy_type="http", proxy_host="127.0.0.1",
    )
    _inline(queue)
    created = _workspaces(monkeypatch, tmp_path)
    resolved, failed = [], []
    errors = _errors(queue)
    meta = _plan(rpc)

    handle = queue.resolve_magnet_preflight(
        _magnet(meta.info_hash), on_resolved=resolved.append, on_failed=failed.append,
    )

    assert rpc.metadata_added == [] and created == []
    assert resolved == [] and failed == [handle]
    assert errors == [qm.TORRENT_SUPPORT_DISABLED] and not pending
    _empty(queue, rpc, db_path)


@pytest.mark.parametrize("refusal", ["invalid", "held", "disabled", "live"])
def test_requests_refusable_without_network_do_not_ask_for_consent(
    queue_env, monkeypatch, tmp_path, notices, refusal,
):
    host, queue, rpc, db_path, contents, loading, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
        torrent_support_enabled=(refusal != "disabled"),
    )
    consent = _Consent(monkeypatch)
    created = _workspaces(monkeypatch, tmp_path)
    errors = _errors(queue)
    meta = _plan(rpc)
    if refusal == "held":
        # Model another request's ownership; this refusal must preserve it.
        queue._preflight_hashes.add(meta.info_hash)
        url = _magnet(meta.info_hash)
    elif refusal == "disabled":
        url = _magnet(meta.info_hash)
    elif refusal == "live":
        monkeypatch.setattr(queue, "_live_torrent", lambda info_hash: info_hash == meta.info_hash)
        url = _magnet(meta.info_hash)
    else:
        url = "magnet:?xt=urn:btih:nothex"
    resolutions, failures = [], []
    original = queue.resolve_magnet_preflight

    def resolve(*args, **kwargs):
        original_failed = kwargs.get("on_failed")

        def failed(handle):
            failures.append((handle, len(resolutions)))
            if original_failed is not None:
                original_failed(handle)

        kwargs["on_failed"] = failed
        handle = original(*args, **kwargs)
        resolutions.append(handle)
        return handle

    monkeypatch.setattr(queue, "resolve_magnet_preflight", resolve)

    host.add_urls_checked([url])

    assert notices == []
    assert consent.calls == [] and rpc.metadata_added == [] and created == []
    assert loading.calls == [] and contents.calls == [] and not pending
    assert len(errors) == 1 and _rows(db_path) == [] and queue.tasks == {}
    assert queue._preflight_hashes == ({meta.info_hash} if refusal == "held" else set())
    assert queue._magnet_resolutions == {} and queue._consent_granted == set()
    assert len(resolutions) == 1 and failures == [(resolutions[0], 0)]
    assert resolutions[0].state == (qm.MAGNET_DUPLICATE if refusal in {"held", "live"} else qm.MAGNET_ERROR)
    if refusal == "disabled":
        assert errors == [qm.TORRENT_SUPPORT_DISABLED]
    elif refusal == "held":
        assert errors == [qm.TORRENT_PREFLIGHT_PENDING]
    elif refusal == "live":
        assert errors == ["That torrent is already in Cove's queue."]


@pytest.mark.parametrize("change,message", [
    pytest.param({"torrent_support_enabled": False}, qm.TORRENT_SUPPORT_DISABLED,
                 id="disabled"),
    pytest.param({"torrent_ip_disclosure_shown": False}, CONSENT_REQUIRED,
                 id="consent-revoked"),
])
def test_p8_worker_rechecks_support_and_persisted_consent(
    queue_env, monkeypatch, tmp_path, notices, change, message,
):
    _host, queue, rpc, db_path, _c, _l, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path,
    )
    created = _workspaces(monkeypatch, tmp_path)
    resolved, failed = [], []
    errors = _errors(queue)
    meta = _plan(rpc)
    handle = queue.resolve_magnet_preflight(
        _magnet(meta.info_hash), on_resolved=resolved.append, on_failed=failed.append,
    )
    for name, value in change.items():
        setattr(queue.settings, name, value)

    deliver()

    assert len(rpc.metadata_added) == 0, f"metadata admissions after {change}: expected 0, observed {len(rpc.metadata_added)}"
    assert handle.state == "blocked" and failed == [handle] and resolved == []
    assert errors == [message] and all(not path.exists() for path in created)
    assert not pending
    _empty(queue, rpc, db_path)


@pytest.fixture
def granted_request(queue_env, monkeypatch, tmp_path):
    _host, queue, rpc, db_path, _c, _l, _pending, _deliver = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    _inline(queue)
    meta = _plan(rpc)
    requests = []
    handle = queue.resolve_magnet_preflight(
        _magnet(meta.info_hash), p2p_consent_granted=True,
        on_resolved=requests.append,
    )
    assert len(rpc.metadata_added) == 1 and len(requests) == 1
    return queue, rpc, db_path, handle, requests[0]


@pytest.mark.parametrize("value", [
    pytest.param("false", id="false-string"), pytest.param("yes", id="yes-string"),
    pytest.param(1, id="one"), pytest.param(2, id="two"),
    pytest.param(object(), id="object"), pytest.param([True], id="list"),
])
@pytest.mark.parametrize("boundary", ["resolver", "evaluator", "commit"])
def test_r2_authorization_requires_literal_true(
    queue_env, monkeypatch, tmp_path, value, boundary,
):
    _host, queue, rpc, db_path, _c, _l, pending, _d = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    _inline(queue)
    meta = _plan(rpc)
    url = _magnet(meta.info_hash)
    if boundary == "evaluator":
        assert queue.local_p2p_policy(consent_granted=value) == qm.P2P_NEEDS_CONSENT, "non-True request consent returned ALLOW"
    elif boundary == "commit":
        prepared = queue.prepare_url(url)
        tid = queue.commit_prepared(prepared, p2p_consent_granted=value)
        assert tid not in queue._consent_granted, "non-True request consent seeded a task grant"
        assert len(_rows(db_path)) == 1
    else:
        created = _workspaces(monkeypatch, tmp_path)
        errors = _errors(queue)
        resolved, failed = [], []
        handle = queue.resolve_magnet_preflight(
            url, p2p_consent_granted=value,
            on_resolved=resolved.append, on_failed=failed.append,
        )
        assert len(rpc.metadata_added) == 0, "non-True request consent admitted metadata RPC"
        assert handle.state == qm.MAGNET_BLOCKED and failed == [handle] and resolved == []
        assert errors == [CONSENT_REQUIRED] and created == [] and not pending
        assert not handle.p2p_consent_grant.allowed
        _empty(queue, rpc, db_path)


def test_r2_post_insert_exception_preserves_surviving_task_consent(
    granted_request, monkeypatch,
):
    queue, rpc, db_path, _handle, request = granted_request
    original = queue._maybe_start_next

    def fail_schedule():
        assert len(_rows(db_path)) == 1
        raise RuntimeError("post-insert scheduling failed")

    monkeypatch.setattr(queue, "_maybe_start_next", fail_schedule)
    with pytest.raises(RuntimeError, match="post-insert scheduling failed"):
        queue.commit_torrent_preflight(request)
    tid = _rows(db_path)[0]["id"]
    assert tid in queue.tasks
    assert tid in queue._consent_granted, "post-insert exception discarded surviving task consent"
    monkeypatch.setattr(queue, "_maybe_start_next", original)
    prompts = []
    queue.torrent_consent_needed.connect(prompts.append)
    _running(queue)
    queue._maybe_start_next()
    assert prompts == [] and len(rpc.torrents) == 1


@pytest.mark.parametrize("duplicate", [False, True], ids=["conversion", "duplicate-drop"])
def test_r2_cached_conversion_retires_source_task_consent(
    granted_request, monkeypatch, duplicate,
):
    from cove import debrid

    queue, rpc, db_path, _handle, request = granted_request
    tid = queue.commit_torrent_preflight(request)
    assert tid in queue._consent_granted
    cached = _fixture_cached(request.metadata, [("A.bin",), ("B.bin",), ("Folder", "C.bin")])
    if duplicate:
        prepared = queue.prepare_url(
            cached.files[0].locked_link, source_type=qm.SOURCE_TORRENT_FILE,
            info_hash=cached.info_hash, debrid_route=cached.provider,
        )
        queue.commit_prepared(prepared)
    monkeypatch.setattr(debrid, "resolve_torrent", lambda *a, **k: cached)
    queue._launch(queue.tasks[tid])

    assert tid not in queue._consent_granted, "cached conversion/drop retained source task consent"
    rows = _rows(db_path)
    assert all(row["source_type"] == qm.SOURCE_TORRENT_FILE for row in rows)
    assert len(rows) == (1 if duplicate else 3)
    assert (tid not in queue.tasks) if duplicate else (queue.tasks[tid].source_type == qm.SOURCE_TORRENT_FILE)
    assert rpc.torrents == [] and rpc.magnets == []


def test_r2_revocation_discarded_request_cannot_seed_a_later_commit(granted_request):
    queue, _rpc, _db_path, _handle, request = granted_request
    queue.discard_torrent_preflight(request)
    tid = queue.commit_torrent_preflight(request)
    assert tid not in queue._consent_granted, "discarded request seeded stale consent"
    assert not request.p2p_consent_grant.allowed


@pytest.mark.parametrize("outcome", ["cancel", "failure"])
def test_r2_revocation_terminal_resolution_cannot_seed_retained_preflight(
    queue_env, monkeypatch, tmp_path, outcome,
):
    _host, queue, rpc, _db_path, _c, _l, pending, deliver = _env(
        queue_env, monkeypatch, tmp_path, torrent_ip_disclosure_shown=False,
    )
    meta = _plan(rpc, add_error=(outcome == "failure"))
    url = _magnet(meta.info_hash)
    prepared = queue.prepare_url(url)
    resolved, failed = [], []
    handle = queue.resolve_magnet_preflight(
        url, p2p_consent_granted=True,
        on_resolved=resolved.append, on_failed=failed.append,
    )
    # Model a retained late completion holding this resolution's same token.
    retained = qm.TorrentPreflight(
        metadata=meta, prepared=prepared,
        p2p_consent_grant=handle.p2p_consent_grant,
    )
    if outcome == "cancel":
        assert handle.cancel()
    deliver()
    assert handle.state == (qm.MAGNET_CANCELLED if outcome == "cancel" else qm.MAGNET_ERROR)
    assert failed == [handle] and resolved == [] and not pending
    assert not handle.p2p_consent_grant.allowed, "terminal resolution retained consent"
    assert queue._preflight_hashes == set() and queue._magnet_resolutions == {}
    tid = queue.commit_torrent_preflight(retained)
    assert tid not in queue._consent_granted


def test_r2_revocation_request_grant_is_consumed_once(granted_request):
    queue, _rpc, db_path, _handle, request = granted_request
    first = queue.commit_torrent_preflight(request)
    second = queue.commit_torrent_preflight(request)
    assert first != second and len(_rows(db_path)) == 2
    assert queue._consent_granted == {first}
    assert not request.p2p_consent_grant.allowed


@pytest.mark.parametrize("outcome", ["destination", "transaction", "unreconciled"])
def test_r3_failed_cached_conversion_retains_request_consent(
    granted_request, monkeypatch, outcome,
):
    from contextlib import contextmanager
    from cove import debrid

    queue, rpc, db_path, _handle, request = granted_request
    tid = queue.commit_torrent_preflight(request)
    task = queue.tasks[tid]
    cached = _fixture_cached(request.metadata, [("A.bin",), ("B.bin",)])
    before = [dict(row) for row in _rows(db_path)]

    if outcome == "destination":
        with monkeypatch.context() as patch:
            patch.setattr(qm, "_within", lambda *args: False)
            patch.setattr(debrid, "resolve_torrent", lambda *args, **kwargs: cached)
            queue._launch(task)
        assert task.status == "error"
        assert task.error == "This torrent contains a file path Cove will not write to."
    elif outcome == "transaction":
        real_connect = qm.db.connect

        class FailingConversion:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, parameters=()):
                if sql.lstrip().startswith("UPDATE downloads"):
                    raise RuntimeError("conversion transaction failed")
                return self.conn.execute(sql, parameters)

        @contextmanager
        def fail_conversion():
            with real_connect() as conn:
                yield FailingConversion(conn)

        with monkeypatch.context() as patch:
            patch.setattr(qm.db, "connect", fail_conversion)
            with pytest.raises(RuntimeError, match="conversion transaction failed"):
                queue._materialize_cached_torrent(task, cached, cached.files)
    else:
        task.launch_gid = "reserved-unreconciled"
        queue._materialize_cached_torrent(task, cached, cached.files)
        assert task.status == "error" and task.error == queue._UNRECONCILED_TORRENT_ERROR

    assert tid in queue._consent_granted, f"{outcome} failure discarded surviving task consent"
    assert task.source_type == qm.SOURCE_TORRENT
    rows = _rows(db_path)
    assert len(rows) == 1 and rows[0]["source_type"] == qm.SOURCE_TORRENT
    if outcome == "transaction":
        assert [dict(row) for row in rows] == before
        assert task.status == "queued"
    else:
        assert rows[0]["status"] == "error"
    assert rpc.torrents == [] and rpc.magnets == []

    if outcome == "destination":
        prompts = []
        queue.torrent_consent_needed.connect(prompts.append)
        monkeypatch.setattr(debrid, "resolve_torrent", lambda *args, **kwargs: None)
        _running(queue)
        queue.resume(tid)
        assert prompts == [] and len(rpc.torrents) == 1
