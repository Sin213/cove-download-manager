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
def test_family_host_refuses_before_primary_or_probe(monkeypatch, url):
    calls = []
    monkeypatch.setattr(host, "deliver_to_primary", lambda request: calls.append(request) or True)
    monkeypatch.setattr(netiface, "bound_requests_session", lambda name: pytest.fail("unapproved probe"))
    result = host.handle_message({"action": "download", "url": url}, None, Settings(gofile_api_token=TOKEN))
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
    payload = bytes(range(256)) * 2048
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
            self.send_response((206 if start or end + 1 != len(content) else 200) if allowed else 403)
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Content-Type", "application/octet-stream")
            if start or end + 1 != len(content):
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(content)}")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content[start:end + 1])

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
