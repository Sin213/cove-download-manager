"""Browser takeover proofs. All HTTP is fake or lead-run loopback storage."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from cove import db, gofile, native_messaging as host, netiface, single_instance as ipc
from cove.app import BrowserDownloadGate
from cove.config import Settings
from tests.test_queue import queue_env  # noqa: F401
from tests.test_queue_output_ownership import inline
from tests.test_gofile import Resp, TOKEN, LANDING
from tests.test_gofile_relay import loopback_storage, URL as RELAY_URL  # noqa: F401

URL = "https://store-eu-par-6.gofile.io/download/web/11111111-aaaa-4aaa-8aaa-111111111111/movie.mp4"
SHARE = "https://gofile.io/d/ObvAFn9h"
FAMILY_PAGES = [
    "https://gofile.io/file", "https://other.gofile.io/file", "https://api.gofile.io/file",
    "https://store-1.gofile.io/download/abcdef/movie.mp4",
    "https://store-1.gofile.io/download/direct/abcdef/movie.mp4",
    "https://STORE-1.GOFILE.IO/file", "https://store-1.gofile.io./file",
    "https://bücher.gofile.io/file", "https://ｇｏｆｉｌｅ.io/file",
    "https://gofile。io/file", "https://%67ofile.io/file", "https://store-1.%67ofile%2eio/file",
    URL.replace("https:", "http:"), URL.replace("https:", "ftp:"),
    URL.replace(".io/", ".io:444/"), URL.replace("https://", "https://user@"),
    URL + "?token=not-a-real-token", URL + "#fragment", URL.replace(".io/", ".io:/"),
    "https://store-1.gofile.io:bad/file", "https://[gofile.io/file",
    "https:///gofile.io/file", "https:/gofile.io/file", "https://gofile.io\\@other.test/file",
]


class Session:
    def __init__(self, *responses):
        self.responses, self.calls, self.closed = list(responses), [], False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def probe(session, deadline=None):
    verify = getattr(gofile, "verify_browser_download", None)
    assert callable(verify), "browser takeover requires an access proof"
    return verify(URL, "movie.mp4", Settings(gofile_api_token=TOKEN), session=session,
                  deadline=time.monotonic() + 1 if deadline is None else deadline)


def test_classifier_accepts_only_store_delivery():
    classify = getattr(gofile, "is_delivery_url", None)
    assert callable(classify), "delivery URLs need a strict classifier"
    assert classify(URL) and classify(URL.replace("store-eu-par-6", "store"))
    for value in [SHARE, URL + "?x", URL + "?", URL + "#", URL + "#x",
                  URL.replace("https:", "http:"), URL.replace(".io/", ".io:444/"),
                  URL.replace(".io/", ".io.evil/"), URL.replace("store-eu-par-6", "api"),
                  URL.replace("https://", "https://u@"), URL.replace("store-eu-par-6", "store--bad"),
                  URL + "/extra", URL + " ", URL.replace(".io/", ".io\\evil/"),
                  URL.replace("gofile", "gofıle"), URL.replace("store-eu-par-6", "xn--store-9za"),
                  URL.replace("store-eu-par-6", "%73tore-eu-par-6"),
                  URL.rsplit("/", 1)[0] + "/.", URL.rsplit("/", 1)[0] + "/%2e%2e",
                  URL.replace(".io/", ".io:/"), URL.replace("https://", "https:///"), URL + "\n"]:
        assert not classify(value), repr(value)


STORAGE_NODE_LABELS = ["store", "store3", "store12", "store-eu-par-6", "store-na-phx-1", "store3-eu",
    "cold-na-phx-8", "zone-1", "a1", "evilstore3", "store3x", "www1", "upload1"]
INVALID_STORAGE_AUTHORITIES = [
    "store3.gofile.io.evil.test", "www.gofile.io", "api.gofile.io", "upload.gofile.io", "uploads.gofile.io",
    "gofile.io", "a.b.gofile.io", "cold.na.gofile.io", "-cold.gofile.io", "cold_8.gofile.io",
    "cold-.gofile.io", "cold--8.gofile.io", "1evil.gofile.io", "cold-na-phx-8.gofile.io.evil.test",
    "evilgofile.io", "cold8.gofile.io:444", "a" * 64 + ".gofile.io",
    "store3.gofile.io:444", "store..gofile.io", "-store3.gofile.io", "store_3.gofile.io",
]


@pytest.mark.parametrize("label", STORAGE_NODE_LABELS)
def test_storage_node_delivery_predicates(label):
    target = URL.replace("store-eu-par-6", label)
    assert gofile.is_delivery_url(target)
    assert gofile.is_relay_delivery_url(target)


@pytest.mark.parametrize("label", STORAGE_NODE_LABELS)
def test_storage_node_host(monkeypatch, label):
    target = URL.replace("store-eu-par-6", label)
    session = Session(Resp(headers={"Content-Length": "5"}, body=b"video"))
    calls = []
    monkeypatch.setattr(gofile, "upstream_session", lambda _: session)
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: calls.append(request) or True)
    result = host.handle_message({"action":"download_gofile_relay", "url":target,
        "filename":"movie.mp4", "gofileToken":TOKEN}, None, Settings())
    assert result == {"status":"ok", "proof":"gofile-relay-v1"}
    assert calls[0]["url"] == target and calls[0]["gofileToken"] == TOKEN
    assert session.calls[0][0] == target


@pytest.mark.parametrize("label", STORAGE_NODE_LABELS)
def test_storage_node_gate(relay_queue, label):
    queue, rpc, gate, request = relay_queue
    assert gate.accept({**request, "url":URL.replace("store-eu-par-6", label)})
    assert gate.last_gofile_relay_ready
    assert len(queue.tasks) == len(queue._gofile_caps) == 1


@pytest.mark.parametrize("authority", [*INVALID_STORAGE_AUTHORITIES, "COLD-NA-PHX-8.gofile.io"])
def test_storage_node_lookalikes_refused(relay_queue, monkeypatch, authority):
    target = URL.replace("store-eu-par-6.gofile.io", authority)
    queue, rpc, gate, request = relay_queue
    assert not gofile.is_relay_delivery_url(target)
    if authority != "COLD-NA-PHX-8.gofile.io":
        assert not gofile.is_delivery_url(target)
    monkeypatch.setattr(gofile, "upstream_session", lambda _: pytest.fail("refused host must not probe"))
    monkeypatch.setattr(host, "deliver_to_primary", lambda _: pytest.fail("refused host must not hand off"))
    assert host.handle_message({"action":"download_gofile_relay", "url":target,
        "gofileToken":TOKEN}, None, Settings())["status"] == "error"
    assert not gate.accept({**request, "url":target})
    assert not queue.tasks and not rpc.added


@pytest.mark.parametrize("target", [
    URL.replace("store-eu-par-6", "cold-na-phx-8").replace("https:", "http:"),
    URL.replace("store-eu-par-6", "user@cold-na-phx-8"),
    URL.replace("store-eu-par-6", "cold-na-phx-8") + "?query=1",
    URL.replace("store-eu-par-6", "cold-na-phx-8") + "#fragment",
    URL.replace("store-eu-par-6", "cold-na-phx-8").replace("/download/web/", "/download/direct/"),
    URL.replace("store-eu-par-6", "cold-na-phx-8").replace("/download/web/", "/web/"),
])
def test_storage_node_url_constraints(target):
    assert not gofile.is_delivery_url(target)
    assert not gofile.is_relay_delivery_url(target)


def test_host_without_token_never_contacts_primary(monkeypatch):
    calls = []
    monkeypatch.setattr(host, "deliver_to_primary", lambda r: calls.append(r) or True)
    result = host.handle_message({"action": "download", "url": URL}, None, Settings())
    assert result["status"] == "error", "unverified acceptance would cancel Firefox"
    assert result["reason"] == "gofile_needs_token"
    assert "Firefox" in result["message"] and "Premium" not in result["message"]
    assert calls == []


@pytest.mark.parametrize("url", [SHARE, "https://gofile.io/", "https://www.gofile.io/account"])
def test_host_pages_never_contact_primary(monkeypatch, url):
    calls = []
    monkeypatch.setattr(host, "deliver_to_primary", lambda r: calls.append(r) or True)
    result = host.handle_message({"action": "download", "url": url}, None, Settings())
    assert result.get("reason") == "gofile_page" and calls == []


@pytest.mark.parametrize("url", FAMILY_PAGES)
@pytest.mark.parametrize("action", ["download", "download_gofile_relay"])
def test_family_host_refuses_before_primary_or_probe(monkeypatch, url, action):
    calls = []
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: calls.append(request) or True)
    monkeypatch.setattr(netiface, "bound_requests_session", lambda name: pytest.fail("unapproved probe"))
    result = host.handle_message({"action": action, "url": url, "gofileToken": TOKEN}, None, Settings(gofile_api_token=TOKEN))
    assert result.get("reason") == "gofile_page" and result["status"] == "error"
    assert not calls


@pytest.mark.parametrize("url", FAMILY_PAGES)
def test_family_gate_refuses_without_add_or_row(queue_env, monkeypatch, url):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    calls = []
    monkeypatch.setattr(queue, "add_url", lambda *a, **k: calls.append(a) or 123)
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    assert gate.accept({"url": url}) is False
    assert gate.last_reason == "gofile_page" and not calls and not queue.tasks and not rpc.added
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0


def test_manual_queue_intake_of_non_share_family_url_is_unchanged(queue_env):
    queue, _, _ = queue_env()
    url = "https://other.gofile.io/download/direct/abcdef/movie.mp4"
    task_id = queue.add_url(url, filename="movie.mp4")
    assert task_id is not None
    assert queue.tasks[task_id].url == url and queue.tasks[task_id].source_type == ""


def test_family_predicate_keeps_lookalikes_and_path_mentions_out():
    assert all(gofile.is_gofile_family(url) for url in [URL, *FAMILY_PAGES])
    for url in ["https://evil-gofile.io/file", "https://gofile.io.evil.test/file",
                "https://example.test/gofile.io/file", "https://gofile.io@other.test/file",
                "file://gofile.io/file", None]:
        assert not gofile.is_gofile_family(url)
    assert ipc.reply_category({"category": "gofile_page"}) == "gofile_page"


def test_probe_learns_size_and_closes_response():
    response = Resp(status=206, headers={"Content-Range": "bytes 0-4095/98765"}, body=b"video")
    session = Session(response)
    assert probe(session) == 98765 and response.closed
    assert session.calls[0][1]["headers"]["Cookie"] == f"accountToken={TOKEN}"
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["headers"]["Range"] == "bytes=0-4095"


@pytest.mark.parametrize("response, reason", [
    (Resp(status=401), "gofile_denied"), (Resp(status=403), "gofile_denied"),
    (Resp(status=404), "gofile_gone"), (Resp(status=410), "gofile_gone"),
    (Resp(status=429), "gofile_rate_limited"), (Resp(status=503), "gofile_unavailable"),
    (Resp(body=LANDING), "gofile_page"), (Resp(body=b'{"status":"error-token"}'), "gofile_page"),
    (Resp(headers={"Content-Type": "text/html"}, body=b"login"), "gofile_page"),
    (Resp(status=302, headers={"Location": "https://gofile.io/"}), "gofile_page"),
    (Resp(headers={"Content-Length": "0"}), "gofile_empty"), (Resp(), "gofile_empty"),
    *[(Resp(status=206, headers={"Content-Range": r}), "gofile_unavailable") for r in
      ["bytes 0-0/0", "bytes 5-9/100", "bytes 0-9/5", "bytes */100", "bytes 0-1/" + "9" * 5000, ""]],
    (Resp(status=416, headers={"Content-Range": "bytes */0"}), "gofile_unavailable"),
])
def test_probe_refusals_are_fixed(response, reason):
    with pytest.raises(gofile.GofileError) as error:
        probe(Session(response, Resp(body=LANDING)))
    assert error.value.code == reason and "Firefox" in str(error.value)
    assert TOKEN not in str(error.value) and URL not in str(error.value)


@pytest.mark.parametrize("response", [Resp(status=401), Resp(status=403), Resp(body=LANDING),
    Resp(body=b'{"status":"error-token"}'), Resp(status=302, headers={"Location": "https://gofile.io/"}),
    Resp(headers={"Content-Length": "0"})])
def test_failed_host_probe_cannot_contact_primary(monkeypatch, response):
    session = Session(response, Resp(body=LANDING))
    calls, logs = [], []
    monkeypatch.setattr(netiface, "bound_requests_session", lambda name: session)
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: calls.append(request) or True)
    monkeypatch.setattr(host, "_log", lambda *a, **k: logs.append((a, k)))
    result = host.handle_message({"action": "download", "url": URL, "filename": "movie.mp4"},
                                 None, Settings(gofile_api_token=TOKEN))
    assert result["status"] == "error" and result["reason"] in ("gofile_denied", "gofile_page", "gofile_empty")
    assert not calls and session.closed
    for secret in (TOKEN, URL, gofile.token_fingerprint(TOKEN)):
        assert secret not in repr(logs) + json.dumps(result)


@pytest.mark.parametrize("target", ["https://evil.test/file", "http://store-1.gofile.io/file",
    "https://gofile.io:444/file", "https://user@gofile.io/file", "https://gofile.io\\@evil.test/file",
    "https://gofile.io.evil.test/file", "https://gofıle.io/file", "https://xn--gofle-cta.io/file",
    "https:///gofile.io/file"])
def test_probe_never_sends_token_to_unsafe_redirect(target):
    session = Session(Resp(status=302, headers={"Location": target}))
    with pytest.raises(gofile.GofileError):
        probe(session)
    assert len(session.calls) == 1


@pytest.mark.parametrize("fp, held", [(None, "current"), ("wrong", "current"),
                                     ("current", "stale"), ("current", None)])
def test_gate_refuses_before_creating_a_task(queue_env, fp, held):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    current = gofile.token_fingerprint(TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: current if held == "current" else held
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    assert gate.accept({"url": URL, "gofile_fp": current if fp == "current" else fp}) is False
    assert not queue.tasks and not rpc.added


def test_fingerprint_schema():
    assert "gofile_fp" not in ipc.validate_browser_download({"url": URL})
    fp = gofile.token_fingerprint(TOKEN)
    assert ipc.validate_browser_download({"url": URL, "gofile_fp": fp}).get("gofile_fp") == fp
    for bad in ["a" * 63, "g" * 64, 1, "a" * 65, ""]:
        with pytest.raises(ipc.MessageError) as error:
            ipc.validate_browser_download({"url": URL, "gofile_fp": bad})
        assert error.value.category == "invalid_schema"


@pytest.mark.parametrize("size", [-1, True, "1", None, 2**62 + 1])
def test_gate_refuses_invalid_delivery_size(queue_env, size):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    assert not gate.accept({"url": URL, "file_size": size, "gofile_fp": gofile.token_fingerprint(TOKEN)})
    assert gate.last_reason == "invalid_schema" and not queue.tasks


def test_plain_launch_refuses_stale_jar(queue_env):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN, intelligent_segments=False)
    rpc.gofile_cookie_fingerprint = lambda: "stale"
    task = queue.tasks[queue.add_url(URL, filename="movie.mp4")]
    inline(queue)
    queue._launch(task)
    assert task.status == "error" and task.error == gofile.COOKIE_RESTART and not rpc.added


def test_plain_delivery_bypasses_debrid(queue_env, monkeypatch):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN, real_debrid_enabled=True, real_debrid_api_token="fake")
    assert queue._debrid_enabled()
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    task = queue.tasks[queue.add_url(URL, filename="movie.mp4")]
    calls = []
    monkeypatch.setattr("cove.debrid.resolve", lambda *a, **k: calls.append(a))
    assert queue._resolve_debrid(task) == URL
    inline(queue)
    queue._launch(task)
    assert rpc.added[0]["uris"] == [URL]
    assert not calls and task.debrid_provider == ""


def test_overall_budget_covers_a_hanging_body():
    release, ended = threading.Event(), threading.Event()

    class Hanging(Resp):
        def iter_content(self, size):
            release.wait(2)
            ended.set()
            yield b"video"

    start = time.monotonic()
    try:
        with pytest.raises(gofile.GofileError) as error:
            probe(Session(Hanging()), deadline=start + .05)
        assert error.value.code == "gofile_unavailable"
        assert time.monotonic() - start < .5
    finally:
        release.set()
        assert ended.wait(1)


@pytest.mark.parametrize("settings", [None, Settings(gofile_api_token=TOKEN, torrent_network_interface="absent")])
def test_unreadable_config_or_unavailable_interface_never_falls_back(monkeypatch, settings):
    calls = []
    monkeypatch.setattr(host, "deliver_to_primary", lambda r: calls.append(r) or True)
    def unavailable(name):
        raise netiface.NetworkInterfaceUnavailable("PRIVATE_INTERFACE")
    monkeypatch.setattr(netiface, "bound_requests_session", unavailable)
    result = host.handle_message({"action": "download", "url": URL}, None, settings)
    assert result["reason"] == "gofile_unavailable" and not calls
    assert "PRIVATE_INTERFACE" not in json.dumps(result)


@pytest.mark.parametrize("accepted, reason", [(True, None), (False, "gofile_restart"),
    (False, "gofile_engine"), (False, "gofile_token"), (False, "gofile_page"), (False, "app_unavailable")])
def test_verified_host_handoff_is_additive(monkeypatch, accepted, reason):
    session = Session(Resp(status=206, headers={"Content-Range": "bytes 0-4/1234"}, body=b"video"))
    interfaces, requests, logs = [], [], []
    monkeypatch.setattr(netiface, "bound_requests_session", lambda name: interfaces.append(name) or session)
    monkeypatch.setattr(host, "_log", lambda *a, **k: logs.append((a, k)))
    def deliver(request):
        requests.append(request)
        host._LAST_HANDOFF_REASON = reason
        return accepted
    monkeypatch.setattr(host, "deliver_to_primary", deliver)
    result = host.handle_message({"action": "download", "url": URL, "filename": "movie.mp4", "fileSize": -1},
                                 None, Settings(gofile_api_token=TOKEN, torrent_network_interface="vpn-test"))
    assert interfaces == ["vpn-test"] and session.closed
    assert requests[0]["gofile_fp"] == gofile.token_fingerprint(TOKEN)
    assert requests[0]["file_size"] == 1234
    assert result["status"] == ("ok" if accepted else "error")
    if not accepted:
        assert result["reason"] == (reason if reason.startswith("gofile_") else "gofile_unavailable")
    assert TOKEN not in repr(logs) and URL not in repr(logs)
    assert gofile.token_fingerprint(TOKEN) not in repr(logs) + json.dumps(result)


def test_correct_proof_creates_plain_row_without_secrets(queue_env, monkeypatch):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    fp = gofile.token_fingerprint(TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: fp
    logs = []
    monkeypatch.setattr(queue, "_diag", lambda *a, **k: logs.append((a, k)))
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    assert gate.accept({"url": URL, "filename": "movie.mp4", "gofile_fp": fp, "file_size": 1234})
    task = next(iter(queue.tasks.values()))
    assert task.source_type == "" and task.filename == "movie.mp4" and task.expected_size == 1234
    inline(queue)
    queue._launch(task)
    assert task.expected_size == 1234 and rpc.added
    with db.connect() as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM downloads")]
    from urllib.parse import urlsplit
    persisted = urlsplit(rows[0]["url"])
    assert rows[0]["url"] == URL and not (persisted.query or persisted.fragment or persisted.username or persisted.password)
    for evidence in [repr(rows), repr(logs), repr(task)]:
        assert TOKEN not in evidence and fp not in evidence


@pytest.mark.parametrize("body, message, size", [(LANDING, gofile.LANDING_PAGE, None),
    (b'{"status":"error-token"}', gofile.LANDING_PAGE, None),
    *[(b, gofile.SIZE_MISMATCH, 9999) for b in (b"", b"short", b"x" * 10000)]])
def test_plain_delivery_completion_never_publishes_error_body(queue_env, tmp_path, body, message, size):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN, intelligent_segments=False)
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    task = queue.tasks[queue.add_url(URL, filename="movie.mp4")]
    task.expected_size = size
    inline(queue)
    queue._launch(task)
    from pathlib import Path
    output = Path(task.work_dir) / "movie.mp4"
    output.write_bytes(body)
    queue._publish_aria2_output(task, str(output))
    assert task.status == "error" and task.error == message
    assert not output.exists() and not (tmp_path / "movie.mp4").exists()


def test_fingerprint_forwarded_and_primary_refusal_category_preserved(monkeypatch, queue_env):
    fp = gofile.token_fingerprint(TOKEN)
    sent = []
    monkeypatch.setattr(ipc, "_request", lambda name, obj, *args: sent.append(obj) or True)
    assert ipc.send_browser_download("unused", {"url": URL, "gofile_fp": fp})
    assert sent[0]["gofile_fp"] == fp
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    rpc.gofile_cookie_fingerprint = lambda: "stale"
    errors = []
    pending = SimpleNamespace(_server=SimpleNamespace(browser_download_handler=gate.accept),
                              _respond_error=errors.append)
    ipc._PendingConnection._handle_browser_download(pending, {"url": URL, "gofile_fp": fp})
    assert errors == ["gofile_restart"] and not queue.tasks
    assert ipc.reply_category({"category": errors[0]}) == "gofile_restart"


def test_not_ready_gate_cannot_accept(queue_env):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    gate = BrowserDownloadGate()
    gate.queue = queue
    assert not gate.accept({"url": URL, "gofile_fp": gofile.token_fingerprint(TOKEN)})
    assert not queue.tasks


def test_extension_restart_cannot_create_a_second_cove_row(queue_env):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    fp = gofile.token_fingerprint(TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: fp
    request = {"url": URL, "filename": "movie.mp4", "gofile_fp": fp}
    for expected in (True, False):
        gate = BrowserDownloadGate()  # no in-memory interception state survives
        gate.queue, gate.ready = queue, True
        assert gate.accept(request) is expected
    assert len(queue.tasks) == 1


@pytest.mark.parametrize("action", ["download", "download_gofile"])
@pytest.mark.parametrize("verified", [False, True])
def test_primary_acknowledgement_required_for_both_host_actions(monkeypatch, action, verified):
    monkeypatch.setattr(netiface, "bound_requests_session", lambda name: Session(Resp(body=b"video")))
    def primary(name, request, **kwargs):
        if kwargs.get("on_response"):
            kwargs["on_response"]({"ok": True, **({"gofile_verified": True} if verified else {})})
        return True
    monkeypatch.setattr(ipc, "send_browser_download", primary)
    result = host.handle_message({"action": action, "url": URL}, None, Settings(gofile_api_token=TOKEN))
    assert result["status"] == ("ok" if verified else "error")
    assert result.get("proof") == ("gofile-v1" if verified else None)
    if not verified:
        assert result.get("reason") == "gofile_restart"


def test_primary_marks_only_verified_delivery_and_client_exposes_reply(queue_env, monkeypatch):
    from unittest.mock import MagicMock
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    replies = []
    pending = SimpleNamespace(_server=SimpleNamespace(browser_download_handler=gate.accept,
        extension_seen=SimpleNamespace(emit=lambda: None)),
        _respond_ok=lambda accepted, **kwargs: replies.append(kwargs))
    ipc._PendingConnection._handle_browser_download(pending, {"url": URL, "gofile_fp": gofile.token_fingerprint(TOKEN)})
    assert replies == [{"gofile_verified": True}]
    sock = MagicMock()
    sock.waitForConnected.return_value = sock.waitForReadyRead.return_value = True
    sock.readAll.return_value = ipc.encode_message({"ok": True, "gofile_verified": True})
    monkeypatch.setattr(ipc, "QLocalSocket", lambda: sock)
    received = []
    assert ipc.send_browser_download("synthetic", {"url": URL}, on_response=received.append)
    assert received == [{"ok": True, "gofile_verified": True}]


@pytest.mark.parametrize("url", [SHARE, "https://example.test/file"])
def test_new_host_action_never_falls_back_to_plain(monkeypatch, url):
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: pytest.fail("plain handoff"))
    assert host.handle_message({"action": "download_gofile", "url": url}, None, Settings())["reason"] == "gofile_page"


@pytest.mark.parametrize("chunks", [[b"{", b'"status":"error-token"}'], [b"<", b"html>login</html>"]])
def test_fragmented_error_cannot_pass_probe_or_handoff(monkeypatch, chunks):
    class Fragmented(Resp):
        def iter_content(self, size):
            yield from chunks
    monkeypatch.setattr(netiface, "bound_requests_session", lambda name: Session(Fragmented()))
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: pytest.fail("error bytes handed off"))
    result = host.handle_message({"action": "download", "url": URL, "filename": "movie.mp4"},
                                 None, Settings(gofile_api_token=TOKEN))
    assert result.get("reason") == "gofile_page"


def test_accumulated_probe_prefix_is_bounded_and_slow_trickle_times_out():
    count = []
    class Prefix(Resp):
        def iter_content(self, size):
            for _ in range(10):
                count.append(True)
                yield b"x" * 1024
    assert probe(Session(Prefix())) is None and len(count) == 4
    ended = threading.Event()
    class Trickle(Resp):
        def iter_content(self, size):
            try:
                while True:
                    time.sleep(.005)
                    yield b" "
            finally:
                ended.set()
    start = time.monotonic()
    with pytest.raises(gofile.GofileError) as error:
        probe(Session(Trickle()), deadline=start + .1)
    assert error.value.code == "gofile_unavailable" and time.monotonic() - start < .5
    assert ended.wait(1)


@pytest.mark.parametrize("name", ["login.html", "page.htm", "page.xhtml"])
@pytest.mark.parametrize("body", [b"<html>login</html>", b"login"])
def test_browser_html_exemption_removed_but_tab1_keeps_it(queue_env, tmp_path, name, body):
    session = Session(Resp(body=body, headers={"Content-Type": "text/html"}))
    with pytest.raises(gofile.GofileError) as error:
        gofile.verify_browser_download(URL, name, Settings(gofile_api_token=TOKEN), session=session,
                                       deadline=time.monotonic() + 1)
    assert error.value.code == "gofile_page"
    assert gofile._probe(Session(Resp(body=body, headers={"Content-Type": "text/html"})), URL, TOKEN, name) == URL


@pytest.mark.parametrize("name", ["login.html", "page.htm", "page.xhtml"])
def test_browser_html_completion_strict_but_tab1_unchanged(queue_env, name):
    body = b"<html>login</html>"
    for source, status in [("", "error"), ("gofile_file", "completed")]:
        queue, _, _ = queue_env()
        task = queue.tasks[queue.add_url(URL, filename=name)]
        task.source_type, task.expected_size = source, len(body)
        work = queue._prepare_aria2_work(task)
        output = work.path / name
        output.write_bytes(body)
        task.aria2_filename = name
        inline(queue)
        queue._publish_aria2_output(task, str(output))
        assert task.status == status


@pytest.mark.parametrize("body", [b"", b"short", b"x" * 1235])
@pytest.mark.parametrize("proven", [True, False])
def test_proven_size_survives_real_queue_reconstruction(queue_env, body, proven):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    fp = gofile.token_fingerprint(TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: fp
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    assert gate.accept({"url": URL, "filename": "movie.mp4", "gofile_fp": fp, "file_size": 1234 if proven else 0})
    if not proven:  # An adopted/legacy row's observed positive length is restored too.
        task = next(iter(queue.tasks.values()))
        task.total_bytes = 1234
        queue._persist(task)
    restored, rpc2, _ = queue_env(gofile_api_token=TOKEN, intelligent_segments=False)
    rpc2.gofile_cookie_fingerprint = lambda: fp
    task = next(iter(restored.tasks.values()))
    inline(restored)
    restored._launch(task)
    from pathlib import Path
    output = Path(task.work_dir) / "movie.mp4"
    output.write_bytes(body)
    restored._apply_status(task.id, {"status": "active", "totalLength": str(len(body))})
    restored._publish_aria2_output(task, str(output))
    assert task.status == "error" and task.error == gofile.SIZE_MISMATCH and not output.exists()
    with db.connect() as connection:
        assert connection.execute("SELECT total_bytes FROM downloads WHERE id=?", (task.id,)).fetchone()[0] == 1234


def test_token_change_after_acceptance_refuses_launch(queue_env):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN, intelligent_segments=False)
    fp = gofile.token_fingerprint(TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: fp
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    assert gate.accept({"url": URL, "filename": "movie.mp4", "gofile_fp": fp})
    queue.settings.gofile_api_token = "CHANGED_TEST_TOKEN_0123456789"
    inline(queue)
    task = next(iter(queue.tasks.values()))
    queue._launch(task)
    assert task.status == "error" and task.error == gofile.COOKIE_RESTART and not rpc.added


def test_old_primary_rejects_before_admission(queue_env, monkeypatch):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    def old_primary(name, message, *args, **kwargs):
        if message["action"] != "browser_download":
            args[-1]("unknown_action")
            return False
        gate.accept(ipc.validate_browser_download(message))
        kwargs["on_response"]({"ok": True})
        return True
    monkeypatch.setattr(ipc, "_request", old_primary)
    monkeypatch.setattr(netiface, "bound_requests_session", lambda name: Session(Resp(body=b"video")))
    result = host.handle_message({"action": "download_gofile", "url": URL}, None, queue.settings)
    assert result.get("reason") == "gofile_restart" and not queue.tasks
    with db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0


@pytest.mark.parametrize("action,url,accepted", [("browser_download", URL, False),
    ("browser_download_gofile", URL, True), ("browser_download", "https://example.test/file", True)])
def test_distinct_ipc_action_is_required_before_gate(queue_env, action, url, accepted):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    errors, calls = [], []
    pending = SimpleNamespace(_respond_error=errors.append,
        _handle_browser_download=lambda request: calls.append(request) or gate.accept(request))
    ipc._PendingConnection._handle_payload(pending, json.dumps({"version": 1, "action": action,
        "url": url, "gofile_fp": gofile.token_fingerprint(TOKEN)}).encode())
    assert bool(queue.tasks) is accepted and bool(calls) is accepted
    assert not accepted or not errors


@pytest.mark.parametrize("status,headers,size", [
    (200, {"Content-Length": "1234"}, 1234),
    (200, {"Content-Length": "12", "Content-Encoding": "identity"}, 12),
    (200, {"Content-Length": "12", "Content-Range": "bytes 0-11/1234"}, 12),
    (200, {"Content-Range": "bytes 0-0/0"}, None),
    (200, {"Content-Length": "-1"}, None),
    (200, {"Content-Length": "12, 12"}, None), (200, {"Content-Length": "x"}, None),
    (200, {"Content-Length": "12, 13"}, None), (200, {"Content-Length": ""}, None),
    (200, {"Content-Length": str(2**62 + 1)}, None), (200, {"Content-Length": "9" * 5000}, None),
    (200, {"Content-Length": "12", "Content-Encoding": "gzip"}, None),
])
def test_full_response_size_headers_are_safe(status, headers, size):
    assert probe(Session(Resp(status=status, headers=headers, body=b"video"))) == size


def test_zero_observed_length_is_not_restored_as_proven(queue_env):
    queue, _, _ = queue_env()
    queue.add_url(URL)
    restored, _, _ = queue_env()
    assert next(iter(restored.tasks.values())).expected_size is None


def test_full_response_size_reaches_persistence_and_completion(queue_env, monkeypatch):
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN, intelligent_segments=False)
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: gate.accept(request))
    monkeypatch.setattr(netiface, "bound_requests_session", lambda name:
                        Session(Resp(headers={"Content-Length": "1234"}, body=b"video")))
    assert host.handle_message({"action": "download_gofile", "url": URL, "filename": "movie.mp4"},
                                None, queue.settings)["status"] == "ok"
    task = next(iter(queue.tasks.values()))
    inline(queue)
    queue._launch(task)
    from pathlib import Path
    output = Path(task.work_dir) / "movie.mp4"
    output.write_bytes(b"short")
    queue._publish_aria2_output(task, str(output))
    assert task.expected_size == 1234 and task.status == "error" and task.error == gofile.SIZE_MISMATCH
    with db.connect() as connection:
        assert connection.execute("SELECT total_bytes FROM downloads WHERE id=?", (task.id,)).fetchone()[0] == 1234


@pytest.fixture
def browser_storage(tmp_path, monkeypatch, queue_env):
    """Lead-run real aria2 proof; permission errors deliberately fail, not skip.

    Loopback substitutes for the store host as in test_gofile.py. TLS proves
    Secure-cookie delivery, a second hostname proves domain isolation, and an
    HTTP listener proves downgrade isolation. Only test TLS verification is
    disabled; the production jar construction and daemon-loading code run.
    """
    import os
    import shutil
    import socket
    import ssl
    import subprocess
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import requests
    from cove import aria2

    if shutil.which("aria2c") is None:
        pytest.skip("real aria2c is not installed")
    assert ".gofile.io\tTRUE\t/\tTRUE\t" in gofile.cookie_jar_text(TOKEN)
    payload = bytes(range(256)) * 128  # Small fake; every response also has a byte cap.
    state = SimpleNamespace(mode="good", hits=0, requests=[], payload=payload)
    cert, key = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True, timeout=15)
    servers, threads = [], []
    daemon = rpc = queue = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            cookie = self.headers.get("Cookie", "")
            ua = self.headers.get("User-Agent", "")
            state.requests.append((self.path, cookie, ua))
            if self.path == "/file":
                state.hits += 1
            if self.path == "/file" and state.hits > 1 and state.mode in ("redirect", "downgrade"):
                self.send_response(302)
                target = (f"https://localhost:{tls.server_port}/steal" if state.mode == "redirect"
                          else f"http://127.0.0.1:{plain.server_port}/steal")
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            allowed = self.path == "/steal" or f"accountToken={TOKEN}" in cookie
            content = payload
            if not allowed or (state.hits > 1 and state.mode == "html"):
                content = LANDING
            if state.hits > 1 and state.mode == "json":
                content = b'{"status":"error-token"}'
            start, end = 0, len(content) - 1
            ranged = self.headers.get("Range", "")
            if ranged.startswith("bytes=") and allowed:
                first, _, last = ranged[6:].partition("-")
                start = int(first or 0)
                end = min(int(last), end) if last else end
            end = min(end, start + 256 * 1024 - 1)
            self.send_response((206 if start or end + 1 != len(content) else 200) if allowed else 403)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Type", "application/octet-stream")
            if start or end + 1 != len(content):
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content[start:end + 1][:256 * 1024])

    try:
        for secure in (True, False):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            servers.append(server)
            server.daemon_threads = True
            if secure:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            threads.append(thread)
        tls, plain = servers
        local = f"https://127.0.0.1:{tls.server_port}/file"
        for name in list(os.environ):
            if "proxy" in name.lower():
                monkeypatch.delenv(name, raising=False)
        data = tmp_path / "engine-data"
        data.mkdir(mode=0o700)
        monkeypatch.setattr(aria2, "DATA_DIR", data)
        monkeypatch.setattr(aria2, "ARIA2_LOG", data / "aria2.log")
        monkeypatch.setattr(gofile, "COOKIE_DOMAIN", "127.0.0.1")
        monkeypatch.setattr(gofile, "COOKIE_SECURE", True)
        spawn = aria2.Aria2Daemon._spawn
        monkeypatch.setattr(aria2.Aria2Daemon, "_spawn", lambda self, args:
                            spawn(self, args + ["--check-certificate=false"]))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        settings = Settings(rpc_port=port, download_dir=str(tmp_path / "engine-output"), gofile_api_token=TOKEN)
        daemon = aria2.Aria2Daemon(settings)
        daemon.start()
        rpc = aria2.Aria2RPC(settings, timeout=2, secret_provider=daemon.active_rpc_secret,
                             cookie_fingerprint_provider=daemon.gofile_cookie_fingerprint)

        class StorageRPC:
            def __getattr__(self, name):
                return getattr(rpc, name)

            def add_uri(self, urls, *args, **kwargs):
                assert urls == [URL]
                return rpc.add_uri([local], *args, **kwargs)

        class StorageSession:
            def __init__(self):
                self.real = requests.Session()
                self.real.trust_env = False
                self.real.verify = False

            def get(self, url, **kwargs):
                assert url == URL
                return self.real.get(local, **kwargs)

            def close(self):
                self.real.close()

        monkeypatch.setattr(netiface, "bound_requests_session", lambda name: StorageSession())
        queue, _, _ = queue_env(gofile_api_token=TOKEN, intelligent_segments=False)
        queue.rpc = StorageRPC()
        gate = BrowserDownloadGate()
        gate.queue, gate.ready = queue, True
        monkeypatch.setattr(host, "deliver_to_primary", lambda request:
                            gate.accept(ipc.validate_browser_download(request)))
        state.queue, state.rpc = queue, rpc
        state.destination = tmp_path / "final"
        state.admit = lambda: host.handle_message({"action": "download", "url": URL, "filename": "movie.mp4",
            "referrer": SHARE, "fileSize": -1, "directory": str(state.destination)}, None, queue.settings)
        yield state
    finally:
        if queue is not None:
            queue._scheduler_allows = False
            queue._poll.stop()
            queue._ext_poll.stop()
        if daemon is not None:
            daemon.stop()
        if queue is not None:
            queue._pool.waitForDone(2000)
        if rpc is not None:
            rpc.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["good", "html", "json", "redirect", "downgrade"])
def test_real_browser_transfer_and_completion_guard(browser_storage, mode):
    from tests.test_gofile import run_to_end, has_token
    storage = browser_storage
    storage.mode = mode
    assert storage.admit()["status"] == "ok"
    task = next(iter(storage.queue.tasks.values()))
    assert task.filename == "movie.mp4" and task.expected_size == len(storage.payload)
    assert task.source_type == "" and task.url == URL
    run_to_end(storage, storage.queue, task)
    output = storage.destination / "movie.mp4"
    if mode in ("html", "json"):
        assert task.status == "error" and task.error == gofile.LANDING_PAGE
        assert not output.exists()
    else:
        assert task.status == "completed", task.error
        assert output.read_bytes() == storage.payload
    on_host = [cookie for path, cookie, ua in storage.requests if path == "/file" and ua.startswith("aria2")]
    assert on_host and all(has_token(cookie) for cookie in on_host)
    if mode in ("redirect", "downgrade"):
        off_host = [cookie for path, cookie, ua in storage.requests if path == "/steal" and ua.startswith("aria2")]
        assert off_host and all(cookie == "" for cookie in off_host)


def test_relay_host_proof_uses_browser_token_without_api_setting(monkeypatch):
    session = Session(Resp(headers={"Content-Length": "5"}, body=b"video"))
    calls = []
    monkeypatch.setattr(gofile, "upstream_session", lambda _: session)
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: calls.append(request) or True)
    result = host.handle_message({"action": "download_gofile_relay", "url": URL,
        "filename": "movie.mp4", "gofileToken": TOKEN}, None, Settings())
    assert result.get("proof") == "gofile-relay-v1" and result["status"] == "ok"
    assert calls[0]["gofileToken"] == TOKEN and calls[0]["file_size"] == 5
    assert session.calls[0][1]["headers"]["Cookie"] == f"accountToken={TOKEN}"


def test_relay_ipc_validates_distinct_action():
    action, request = ipc.validate_request({"version": ipc.PROTOCOL_VERSION,
        "action": "browser_download_gofile_relay", "url": URL, "gofileToken": TOKEN,
        "filename": "movie.mp4", "file_size": 5})
    assert action == "browser_download_gofile_relay" and request["gofileToken"] == TOKEN


def test_relay_gate_accepts_browser_authority_without_api_token(queue_env, monkeypatch):
    from cove.gofile_relay import GofileRelay
    queue, rpc, _ = queue_env()
    relay = GofileRelay(queue.settings)
    relay._server = SimpleNamespace(server_port=12345)
    monkeypatch.setattr(relay, "start", lambda: None)
    queue.gofile_relay = relay
    rpc.gofile_cookie_fingerprint = lambda: ""
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    assert gate.accept({"url": URL, "gofileToken": TOKEN, "filename": "movie.mp4", "file_size": 5})
    assert next(iter(queue.tasks.values())).source_type == "gofile_browser"


@pytest.mark.parametrize("status", ["active", "paused", "error", "complete"])
@pytest.mark.parametrize("uri", ["http://127.0.0.1:12345/" + "a" * 43 + "/file",
    "http://[::1]:12345/" + "a" * 43 + "/file", "http://127.0.0.1:12345/short/file"])
def test_orphan_relay_never_becomes_a_row(queue_env, status, uri):
    queue, rpc, _ = queue_env()
    queue.gofile_relay._server = SimpleNamespace(server_port=12345)
    rpc.tell_external_snapshot = lambda: [{"gid": "orphan", "status": status,
        "files": [{"uris": [{"uri": uri}]}]}]
    inline(queue)
    queue._check_external()
    assert not queue.tasks


@pytest.fixture
def relay_queue(queue_env, monkeypatch):
    queue, rpc, _ = queue_env()
    queue.gofile_relay._server = SimpleNamespace(server_port=12345)
    monkeypatch.setattr(queue.gofile_relay, "start", lambda: None)
    rpc.gofile_cookie_fingerprint = lambda: ""
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    request = {"url": URL, "gofileToken": TOKEN, "filename": "movie.mp4", "file_size": 5}
    yield queue, rpc, gate, request
    for cap in list(queue._gofile_caps.values()):
        queue.gofile_relay.unregister(cap)


def test_relay_creation_launch_size_resume_and_restart(relay_queue, monkeypatch):
    from cove.queue import QueueManager
    from cove.gofile_relay import FRESH_AUTHORIZATION
    queue, rpc, gate, request = relay_queue
    assert gate.accept(request) and gate.last_gofile_relay_ready
    task = next(iter(queue.tasks.values()))
    cap = queue._gofile_caps[task.id]
    calls = []
    monkeypatch.setattr(rpc, "add_uri", lambda *a, **kw: calls.append((a, kw)) or "gid-relay")
    inline(queue)
    queue._launch(task)
    assert task.status == "paused" and calls[0][1]["options"] == {"no-proxy": "127.0.0.1"}
    assert calls[0][0][0] == [queue.gofile_relay.local_url(cap)]
    assert cap not in repr(task)
    assert TOKEN not in repr(calls)
    task.clear_debrid()
    assert task.expected_size == 5
    queue.resume(task.id)
    assert queue._gofile_caps[task.id] == cap and task.expected_size == 5
    task.total_bytes = 999  # aria2 observations must never replace the proven size on disk.
    queue._persist(task)
    restored = QueueManager(queue.settings, rpc)
    restored._poll.stop(); restored._ext_poll.stop()
    restored._scheduler_allows = False
    inline(restored)
    other = restored.tasks[task.id]
    assert other.expected_size == 5
    restored._launch(other)
    assert other.status == "error" and other.error == FRESH_AUTHORIZATION
    assert not restored._gofile_caps and len(calls) == 1
    with db.connect() as conn:
        persisted = str(tuple(conn.execute("SELECT * FROM downloads").fetchone()))
    assert TOKEN not in persisted and cap not in persisted and "127.0.0.1" not in persisted


@pytest.mark.parametrize("mode", ["engine", "not_ready", "shutdown", "register", "add", "duplicate"])
def test_relay_refusal_has_no_extra_row_or_registration(relay_queue, monkeypatch, mode):
    queue, rpc, gate, request = relay_queue
    if mode == "engine": rpc.gofile_cookie_fingerprint = lambda: None
    if mode == "not_ready": gate.ready = False
    if mode == "shutdown": gate.shutting_down = True
    if mode == "register": monkeypatch.setattr(queue.gofile_relay, "register", lambda *a: (_ for _ in ()).throw(OSError(TOKEN)))
    if mode == "add": monkeypatch.setattr(queue, "add_gofile_browser", lambda *a: None)
    if mode == "duplicate": assert gate.accept(request)
    before = len(queue.tasks), len(queue.gofile_relay._records)
    assert not gate.accept(request) and not gate.last_gofile_relay_ready
    assert before == (len(queue.tasks), len(queue.gofile_relay._records))


@pytest.mark.parametrize("body,size", [(b"<html>login", 0), (b'{"error":true}', 0),
    (b"", 0), (b"vid", 5), (b"video", 5)])
def test_relay_completion_guard_and_registry_cleanup(relay_queue, body, size):
    from pathlib import Path
    queue, rpc, gate, request = relay_queue
    request["file_size"], request["filename"] = size, "page.html"
    assert gate.accept(request)
    task = next(iter(queue.tasks.values()))
    inline(queue)
    work = queue._prepare_aria2_work(task)
    task.aria2_filename = "page.html"
    path = Path(work.path) / "page.html"
    path.write_bytes(body)
    queue._publish_aria2_output(task, str(path))
    assert task.status == ("completed" if body == b"video" else "error")
    assert not queue._gofile_caps and not queue.gofile_relay._records


def test_relay_error_redaction_remove_and_absent_resume(relay_queue):
    from cove.gofile_relay import TRANSFER_FAILED, FRESH_AUTHORIZATION
    queue, rpc, gate, request = relay_queue
    assert gate.accept(request)
    task = next(iter(queue.tasks.values()))
    cap = queue._gofile_caps[task.id]
    logs = []
    queue._diag = lambda *a, **kw: logs.append((a, kw))
    queue._apply_status(task.id, {"status": "error", "errorMessage": cap + TOKEN, "errorCode": cap})
    assert task.error == TRANSFER_FAILED and cap not in repr(logs) and TOKEN not in repr(logs)
    assert not queue._gofile_caps
    task.status, task.gid = "paused", "old-gid"
    queue.resume(task.id)
    assert task.status == "error" and task.error == FRESH_AUTHORIZATION and not rpc.unpaused
    task.gid = None
    inline(queue)
    queue.remove(task.id)
    assert not queue.tasks and not queue.gofile_relay._records


@pytest.mark.parametrize("token", [None, "", "short", "x" * 129, "x;other=y", True])
def test_relay_ipc_rejects_invalid_tokens_and_cross_action(token):
    base = {"version": ipc.PROTOCOL_VERSION, "url": URL, "file_size": 5, "gofileToken": token}
    for action in ("browser_download_gofile_relay", "browser_download", "browser_download_gofile", "open"):
        with pytest.raises(ipc.MessageError):
            ipc.validate_request({**base, "action": action})


@pytest.mark.parametrize("ack", [{}, {"gofile_verified": True}, {"gofile_relay_ready": True},
                                   {"category": "unknown_action"}])
def test_relay_host_requires_exact_primary_ack(monkeypatch, ack):
    monkeypatch.setattr(gofile, "upstream_session", lambda _: Session(Resp(body=b"video", headers={"Content-Length": "5"})))
    def primary(name, request, **kwargs):
        if ack.get("category"):
            kwargs["on_reason"]("unknown_action")
            return False
        kwargs["on_response"]({"ok": True, **ack})
        return True
    monkeypatch.setattr(ipc, "send_browser_download", primary)
    result = host.handle_message({"action": "download_gofile_relay", "url": URL,
        "gofileToken": TOKEN, "filename": "movie.mp4"}, None, Settings())
    assert (result.get("proof") == "gofile-relay-v1") == bool(ack.get("gofile_relay_ready"))


def test_relay_ipc_ack_is_tied_to_gate_and_token_is_forwarded(relay_queue, monkeypatch):
    queue, rpc, gate, request = relay_queue
    replies = []
    pending = SimpleNamespace(_server=SimpleNamespace(browser_download_handler=gate.accept,
        extension_seen=SimpleNamespace(emit=lambda: None)),
        _respond_ok=lambda n, **kw: replies.append(kw))
    ipc._PendingConnection._handle_browser_download(pending, request)
    assert replies == [{"gofile_relay_ready": True}]
    sent = []
    monkeypatch.setattr(ipc, "_request", lambda name, message, *a, **kw: sent.append(message) or True)
    assert ipc.send_browser_download("fake", request)
    assert sent[0]["action"] == "browser_download_gofile_relay" and sent[0]["gofileToken"] == TOKEN


def test_relay_probe_rejects_json_error_before_primary(monkeypatch):
    monkeypatch.setattr(gofile, "upstream_session", lambda _: Session(Resp(body=b'{"error":true}')))
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: pytest.fail("error body admitted"))
    result = host.handle_message({"action": "download_gofile_relay", "url": URL,
        "gofileToken": TOKEN, "filename": "movie.mp4"}, None, Settings())
    assert result.get("reason") == "gofile_page"


def test_relay_rpc_failure_is_redacted_before_any_callback(relay_queue):
    from cove.gofile_relay import TRANSFER_FAILED
    queue, rpc, gate, request = relay_queue
    assert gate.accept(request)
    task = next(iter(queue.tasks.values()))
    task.gid = "gid-sensitive"
    queue._pool = SimpleNamespace(start=lambda call: call.run())
    def fail(gid):
        if gid == "ordinary": raise RuntimeError("ordinary error")
        raise RuntimeError(queue.gofile_relay.local_url(queue._gofile_caps[task.id]) + TOKEN)
    messages = []
    queue._spawn(fail, task.gid, on_fail=messages.append)
    assert messages == [TRANSFER_FAILED]
    queue._spawn(fail, "ordinary", on_fail=messages.append)
    assert messages[-1] == "RuntimeError: ordinary error"


@pytest.mark.parametrize("mode", ["pause", "restart", "redirect", "revoked"])
def test_relay_backend_real_aria2_end_to_end(loopback_storage, queue_env, monkeypatch, tmp_path, mode):
    import shutil
    import socket
    from cove import aria2
    from cove.queue import QueueManager
    from cove.gofile_relay import FRESH_AUTHORIZATION, TRANSFER_FAILED
    from PySide6.QtCore import QCoreApplication
    if shutil.which("aria2c") is None:
        pytest.skip("real aria2c is not installed")
    state = loopback_storage
    state.relay.unregister(state.cap)
    engine = tmp_path / "relay-engine"
    engine.mkdir()
    monkeypatch.setattr(aria2, "DATA_DIR", engine)
    monkeypatch.setattr(aria2, "ARIA2_LOG", engine / "aria2.log")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    queue, _, _ = queue_env(rpc_port=port, proxy_type="http", proxy_host="127.0.0.1", proxy_port=1)
    daemon = aria2.Aria2Daemon(queue.settings)
    rpc = restored = None
    try:
        daemon.start()
        rpc = aria2.Aria2RPC(queue.settings, timeout=2, secret_provider=daemon.active_rpc_secret,
                            cookie_fingerprint_provider=daemon.gofile_cookie_fingerprint)
        queue.rpc, queue.gofile_relay = rpc, state.relay
        inline(queue)
        gate = BrowserDownloadGate()
        gate.queue, gate.ready = queue, True
        monkeypatch.setattr(gofile, "upstream_session", lambda _: state.relay.session_factory())
        def deliver(request):
            _, payload = ipc.validate_request({"version": ipc.PROTOCOL_VERSION,
                "action": "browser_download_gofile_relay", **request})
            return gate.accept(payload)
        monkeypatch.setattr(host, "deliver_to_primary", deliver)
        reply = host.handle_message({"action": "download_gofile_relay", "url": RELAY_URL,
            "gofileToken": TOKEN, "filename": "movie.mp4"}, None, queue.settings)
        assert reply.get("proof") == "gofile-relay-v1"
        task = next(iter(queue.tasks.values()))
        cap = queue._gofile_caps[task.id]
        state.mode = "redirect" if mode == "redirect" else "slow"
        queue._scheduler_allows = True
        queue.force_start(task.id)
        assert task.gid and TOKEN not in str(rpc._call("aria2.getOption", [task.gid]))
        if mode != "redirect":
            assert state.started.wait(3)
            if mode in ("pause", "restart"):
                queue.pause(task.id)
                assert task.status == "paused" and queue._gofile_caps[task.id] == cap
            state.mode = "revoked" if mode == "revoked" else "good"
            state.release.set()
            if mode == "restart":
                queue.stop_gofile_relay()
                restored = QueueManager(queue.settings, rpc)
                restored._poll.stop(); restored._ext_poll.stop()
                other = restored.tasks[task.id]
                restored._launch(other)
                assert other.status == "error" and other.error == FRESH_AUTHORIZATION
                return
            if mode == "pause": queue.resume(task.id)
        deadline = time.monotonic() + 15
        while task.status not in ("completed", "error") and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            queue._apply_status(task.id, rpc.tell_status(task.gid))
            time.sleep(.02)
        assert task.status == ("completed" if mode == "pause" else "error")
        if mode != "pause": assert task.error == TRANSFER_FAILED
        assert not queue._gofile_caps and not state.relay._records
        assert all(path == "/file" and headers.get("Cookie") == f"accountToken={TOKEN}"
                   for path, headers in state.calls)
        assert all(not headers.get("User-Agent", "").startswith("aria2") for _, headers in state.calls)
        with db.connect() as conn:
            persisted = str([tuple(row) for row in conn.execute("SELECT * FROM downloads")])
        assert TOKEN not in persisted and cap not in persisted
    finally:
        state.release.set()
        queue._scheduler_allows = False
        queue._poll.stop(); queue._ext_poll.stop()
        queue.stop_gofile_relay()
        if restored is not None: restored.stop_gofile_relay()
        if rpc is not None: rpc.close()
        daemon.stop()


def test_relay_publication_error_uses_fixed_sentence(relay_queue):
    from cove.gofile_relay import TRANSFER_FAILED
    queue, rpc, gate, request = relay_queue
    assert gate.accept(request)
    task = next(iter(queue.tasks.values()))
    queue._prepare_aria2_work(task)
    inline(queue)
    queue._publish_aria2_output(task, queue.gofile_relay.local_url(queue._gofile_caps[task.id]))
    assert task.error == TRANSFER_FAILED and task.status == "error"


def test_native_loop_drops_previous_token_message_and_response(monkeypatch):
    import io
    import weakref
    class Message(dict): pass
    refs = []
    def decode(stream):
        if refs:
            assert all(ref() is None for ref in refs)
            return None
        msg = Message(action="download_gofile_relay", gofileToken=TOKEN)
        refs.append(weakref.ref(msg))
        return msg
    def handle(*args):
        response = Message(status="error")
        refs.append(weakref.ref(response))
        return response
    monkeypatch.setattr(host, "_LOG", object())
    monkeypatch.setattr(host, "_log", lambda *a, **kw: None)
    monkeypatch.setattr(host, "log_host_start", lambda: None)
    monkeypatch.setattr(host.Settings, "load", lambda: Settings())
    monkeypatch.setattr(host, "Aria2RPC", lambda *a, **kw: None)
    monkeypatch.setattr(host, "_binary_stdio", lambda: (io.BytesIO(), io.BytesIO()))
    monkeypatch.setattr(host, "decode_message", decode)
    monkeypatch.setattr(host, "handle_message", handle)
    host.main()


@pytest.mark.parametrize("size", [None, -1, True, "", "5", 2**62 + 1])
def test_relay_ipc_size_bounds(size):
    with pytest.raises(ipc.MessageError):
        ipc.validate_request({"version": ipc.PROTOCOL_VERSION, "action": "browser_download_gofile_relay",
            "url": URL, "gofileToken": TOKEN, "file_size": size})
    with pytest.raises(ipc.MessageError):
        ipc.validate_request({"version": ipc.PROTOCOL_VERSION, "action": "browser_download",
            "url": URL, "gofileToken": TOKEN, "file_size": 5})


@pytest.mark.parametrize("response", [Resp(body=b"video"),
    Resp(body=b"video", status=206, headers={"Content-Length": "4096", "Content-Range": "bytes 0-4095/10000"}),
    Resp(body=b"video", headers={"Content-Length": "5", "Content-Encoding": "gzip"})])
def test_relay_probe_never_proves_a_response_the_relay_would_refuse(monkeypatch, response):
    monkeypatch.setattr(gofile, "upstream_session", lambda _: Session(response))
    admitted = []
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: admitted.append(request) or True)
    result = host.handle_message({"action": "download_gofile_relay", "url": URL,
        "gofileToken": TOKEN, "filename": "movie.mp4"}, None, Settings())
    assert result["status"] == "error" and not admitted


def test_relay_diagnostics_failure_cannot_turn_accepted_row_into_refusal(relay_queue, monkeypatch):
    from cove import app
    queue, rpc, gate, request = relay_queue
    monkeypatch.setattr(app.diagnostics, "emit", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    assert gate.accept(request) and gate.last_gofile_relay_ready and len(queue.tasks) == 1


def test_restored_relay_failure_does_not_consume_scheduler_slot(relay_queue):
    from cove.queue import QueueManager
    from PySide6.QtCore import QCoreApplication
    queue, rpc, gate, request = relay_queue
    queue.settings.max_concurrent, queue.settings.intelligent_segments = 1, False
    assert gate.accept(request)
    ordinary = queue.add_url("https://example.test/second", filename="second.bin")
    restored = QueueManager(queue.settings, rpc)
    restored._poll.stop(); restored._ext_poll.stop()
    inline(restored)
    restored.resume_persisted()
    deadline = time.monotonic() + .2
    while time.monotonic() < deadline and restored.tasks[ordinary].status == "queued":
        QCoreApplication.processEvents()
    assert restored.tasks[ordinary].gid and restored.tasks[ordinary].status == "active"


@pytest.mark.parametrize("raises", [False, True])
def test_relay_teardown_failure_cannot_skip_daemon_stop(monkeypatch, raises):
    from tests import test_app_launch as oracle
    if raises:
        monkeypatch.setattr(oracle, "_HARNESS", oracle._HARNESS.replace(
            "class FakeQueueManager:\n", "class FakeQueueManager:\n"
            "    def stop_gofile_relay(self): raise RuntimeError('TEARDOWN-CANARY')\n"))
    out, raw = oracle._run_harness([], True, stop_sequence=[True], return_raw=True)
    assert out.get("stop_attempts") == [False] and out["returncode"] == 0
    assert out["constructed"][-2:] == ["shutdown", "diagnostics_shutdown"]
    assert "TEARDOWN-CANARY" not in raw.stderr


@pytest.mark.parametrize("url", [URL.replace("https:", "HTTPS:"), URL.replace("store-eu", "STORE-eu"),
                                   URL.replace("gofile.io", "GOFILE.IO")])
def test_relay_case_variants_refused_at_every_boundary(relay_queue, monkeypatch, url):
    queue, rpc, gate, request = relay_queue
    assert gofile.is_share_url("HTTPS://GOFILE.IO/d/ObvAFn9h")  # Tab 1 remains case tolerant.
    assert gofile._download_url(url)  # Tab 1's resolver keeps its original URL policy.
    monkeypatch.setattr(gofile, "upstream_session", lambda _: Session(Resp(body=b"video", headers={"Content-Length": "5"})))
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: True)
    assert host.handle_message({"action": "download_gofile_relay", "url": url,
        "gofileToken": TOKEN}, None, Settings())["status"] == "error"
    with pytest.raises(ipc.MessageError):
        ipc.validate_request({"version": ipc.PROTOCOL_VERSION, "action": "browser_download_gofile_relay", **request, "url": url})
    assert not gate.accept({**request, "url": url}) and not queue.tasks
    cap = queue.gofile_relay.register(URL, TOKEN, "movie.mp4", 5)
    try: assert queue.add_gofile_browser({**request, "url": url}, cap) is None
    finally: queue.gofile_relay.unregister(cap)
    assert not queue.tasks and not rpc.added


def test_relay_source_never_direct_adds_noncanonical_url(relay_queue, monkeypatch):
    queue, rpc, gate, request = relay_queue
    assert gate.accept(request)
    task = next(iter(queue.tasks.values()))
    task.url = task.url.replace("https:", "HTTPS:")
    calls = []
    monkeypatch.setattr(rpc, "add_uri", lambda *a, **kw: calls.append((a, kw)) or "gid-relay")
    inline(queue)
    queue._launch(task)
    assert not calls or calls[0][0][0] == [queue.gofile_relay.local_url(queue._gofile_caps[task.id])]
    monkeypatch.setattr(queue, "_gofile_live", lambda _: True)  # Defence beyond the initial gate.
    queue._launch(task)
    assert all(a[0] != [task.url] and a[0][0].startswith("http://127.0.0.1:") for a, kw in calls)


@pytest.mark.parametrize("url", [URL, URL.replace("https:", "HTTPS:"),
    URL.replace("store-eu", "STORE-eu"), URL.replace("gofile.io", "GOFILE.IO")])
def test_legacy_delivery_case_policy_matches_base(url):
    assert gofile.is_delivery_url(url)  # All four accepted by f5de0cd.


@pytest.mark.parametrize("action", ["download", "download_gofile"])
def test_legacy_mixed_case_host_ipc_and_gate_keep_delivery_policy(queue_env, monkeypatch, action):
    url = URL.replace("gofile.io", "GOFILE.IO")
    sent = []
    monkeypatch.setattr(netiface, "bound_requests_session", lambda _: Session())
    monkeypatch.setattr(gofile, "verify_browser_download", lambda *a, **kw: 5)
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: sent.append(request) or True)
    assert host.handle_message({"action": action, "url": url}, None,
                               Settings(gofile_api_token=TOKEN))["status"] == "ok"
    assert sent[0]["file_size"] == 5 and sent[0]["gofile_fp"] == gofile.token_fingerprint(TOKEN)
    assert ipc.validate_request({"version": ipc.PROTOCOL_VERSION,
        "action": "browser_download_gofile", **sent[0]})[0] == "browser_download_gofile"
    with pytest.raises(ipc.MessageError, match="gofile_restart"):
        ipc.validate_request({"version": ipc.PROTOCOL_VERSION, "action": "browser_download", "url": url})
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN)
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    gate = BrowserDownloadGate()
    gate.queue, gate.ready = queue, True
    assert gate.accept(sent[0]) and next(iter(queue.tasks.values())).expected_size == 5


def test_saved_legacy_mixed_case_delivery_keeps_size_cookie_and_html_guards(queue_env):
    from pathlib import Path
    queue, rpc, _ = queue_env(gofile_api_token=TOKEN, intelligent_segments=False)
    task = queue.tasks[queue.add_url(URL.replace("gofile.io", "GOFILE.IO"), filename="movie.mp4")]
    task.expected_size = len(LANDING)
    queue._persist(task)
    restored, rpc, _ = queue_env(gofile_api_token=TOKEN, intelligent_segments=False)
    task = next(iter(restored.tasks.values()))
    assert task.expected_size == len(LANDING)
    inline(restored)
    rpc.gofile_cookie_fingerprint = lambda: "stale"
    restored._launch(task)
    assert task.status == "error" and not rpc.added
    rpc.gofile_cookie_fingerprint = lambda: gofile.token_fingerprint(TOKEN)
    restored._launch(task)
    assert rpc.added
    output = Path(task.work_dir) / "movie.mp4"
    output.write_bytes(LANDING)
    restored._publish_aria2_output(task, str(output))
    assert task.status == "error" and task.error == gofile.LANDING_PAGE and not output.exists()
