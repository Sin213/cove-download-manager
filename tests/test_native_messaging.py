"""Tests for the native messaging host protocol and download handling."""
import io
import json
import struct
from unittest.mock import MagicMock, patch

from cove import native_messaging as nm
from cove.native_messaging import (
    encode_message,
    decode_message,
    validate_url,
    handle_message,
)


class _ChunkedStream:
    """Stream that returns at most `chunk` bytes per read, to mimic a pipe
    that delivers a large message in several short reads."""

    def __init__(self, data: bytes, chunk: int = 3):
        self._data = data
        self._pos = 0
        self._chunk = chunk

    def read(self, n: int = -1) -> bytes:
        end = min(self._pos + min(n, self._chunk), len(self._data))
        out = self._data[self._pos:end]
        self._pos = end
        return out


def test_decode_message_handles_short_reads():
    """A large body delivered in small chunks must not be truncated."""
    body = json.dumps({"action": "download", "url": "x" * 5000}).encode("utf-8")
    framed = struct.pack("<I", len(body)) + body
    result = decode_message(_ChunkedStream(framed, chunk=7))
    assert result["action"] == "download"
    assert len(result["url"]) == 5000


def test_decode_message_eof_mid_body_returns_none():
    body = json.dumps({"action": "ping"}).encode("utf-8")
    framed = struct.pack("<I", len(body)) + body[:-2]  # truncated body
    assert decode_message(io.BytesIO(framed)) is None


def test_decode_message_malformed_json_returns_none():
    bad = b"{not json"
    framed = struct.pack("<I", len(bad)) + bad
    assert decode_message(io.BytesIO(framed)) is None


def test_decode_message_zero_length_returns_none():
    framed = struct.pack("<I", 0)
    assert decode_message(io.BytesIO(framed)) is None


def test_sanitize_header_strips_crlf():
    assert nm._sanitize_header("a=b\r\nInjected: x") == "a=bInjected: x"
    assert nm._sanitize_header("clean=1") == "clean=1"
    assert nm._sanitize_header(None) == ""


def test_binary_stdio_uses_existing_buffers():
    """When std streams exist (console/dev), reuse their binary buffers."""
    fake_in = io.BytesIO(b"")
    fake_out = io.BytesIO()

    class _Stream:
        pass

    sin = _Stream()
    sin.buffer = fake_in
    sout = _Stream()
    sout.buffer = fake_out

    with patch.object(nm.sys, "stdin", sin), patch.object(nm.sys, "stdout", sout):
        in_stream, out_stream = nm._binary_stdio()

    assert in_stream is fake_in
    assert out_stream is fake_out


def test_encode_message():
    msg = {"status": "ok"}
    encoded = encode_message(msg)
    length = struct.unpack("<I", encoded[:4])[0]
    body = json.loads(encoded[4:])
    assert length == len(encoded) - 4
    assert body == {"status": "ok"}


def test_decode_message():
    msg = {"action": "ping"}
    body = json.dumps(msg).encode("utf-8")
    data = struct.pack("<I", len(body)) + body
    result = decode_message(io.BytesIO(data))
    assert result == {"action": "ping"}


def test_decode_message_eof():
    result = decode_message(io.BytesIO(b""))
    assert result is None


def test_decode_message_too_large():
    data = struct.pack("<I", 2 * 1024 * 1024) + b"\x00"
    result = decode_message(io.BytesIO(data))
    assert result is None


def test_validate_url_http():
    assert validate_url("https://example.com/file.zip") is True
    assert validate_url("http://example.com/file.zip") is True


def test_validate_url_ftp():
    assert validate_url("ftp://example.com/file.zip") is True


def test_validate_url_blocked_schemes():
    assert validate_url("file:///etc/passwd") is False
    assert validate_url("javascript:alert(1)") is False
    assert validate_url("data:text/html,<h1>hi</h1>") is False


def test_validate_url_garbage():
    assert validate_url("") is False
    assert validate_url("not a url") is False


def test_handle_ping():
    result = handle_message({"action": "ping"}, rpc=None, settings=None)
    assert result["status"] == "ok"
    assert "version" in result


def test_handle_download_invalid_url():
    result = handle_message(
        {"action": "download", "url": "file:///etc/passwd"},
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert result["status"] == "error"


def test_handle_download_missing_url():
    result = handle_message(
        {"action": "download"},
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert result["status"] == "error"


def test_handle_status():
    mock_rpc = MagicMock()
    mock_rpc.tell_active.return_value = [{"gid": "abc", "status": "active"}]
    result = handle_message({"action": "status"}, rpc=mock_rpc, settings=MagicMock())
    assert result["status"] == "ok"
    assert result["downloads"] == [{"gid": "abc", "status": "active"}]
    mock_rpc.tell_active.assert_called_once()


def test_handle_unknown_action():
    result = handle_message({"action": "unknown"}, rpc=None, settings=None)
    assert result["status"] == "error"


# --- fail-open browser delivery -----------------------------------------
#
# The extension cancels the browser's own download the moment this host
# answers {"status": "ok"}. So "ok" must mean a Cove process that is running
# *right now* accepted the download. The host used to write a durable drop
# file and answer ok regardless, which is exactly why a download intercepted
# while Cove was closed reappeared at the next launch.


class _Delivery:
    """Records what the host tried to hand to the running primary."""

    def __init__(self, accept: bool):
        self.accept = accept
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        return self.accept


def test_download_succeeds_only_when_the_primary_accepts(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    delivery = _Delivery(accept=True)
    monkeypatch.setattr(nm, "deliver_to_primary", delivery)

    result = handle_message(
        {
            "action": "download",
            "url": "https://example.invalid/file.zip",
            "filename": "file.zip",
            "referrer": "https://example.invalid/page",
            "cookies": "session=dummy",
            "userAgent": "DummyAgent/1.0",
        },
        rpc=MagicMock(),
        settings=MagicMock(),
    )

    assert result["status"] == "ok"
    assert len(delivery.calls) == 1
    sent = delivery.calls[0]
    assert sent["url"] == "https://example.invalid/file.zip"
    assert sent["filename"] == "file.zip"
    assert sent["referrer"] == "https://example.invalid/page"
    assert sent["cookies"] == "session=dummy"
    assert sent["user_agent"] == "DummyAgent/1.0"


def test_download_preserves_an_explicitly_requested_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    delivery = _Delivery(accept=True)
    monkeypatch.setattr(nm, "deliver_to_primary", delivery)

    handle_message(
        {
            "action": "download",
            "url": "https://www.youtube.com/watch?v=dummyvideoid",
            "filename": "clip.mp4",
            "directory": "/tmp/videos",
        },
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert delivery.calls[0]["directory"] == "/tmp/videos"
    # Extractor/HLS routing is still decided by the queue from the URL, so the
    # host forwards the URL untouched rather than classifying it here.
    assert delivery.calls[0]["url"] == "https://www.youtube.com/watch?v=dummyvideoid"


def test_download_strips_crlf_from_header_values(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    delivery = _Delivery(accept=True)
    monkeypatch.setattr(nm, "deliver_to_primary", delivery)

    handle_message(
        {
            "action": "download",
            "url": "https://example.invalid/f.zip",
            "cookies": "s=1\r\nX-Evil: 1",
            "referrer": "https://example.invalid/\r\nHost: evil",
        },
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    sent = delivery.calls[0]
    assert "\r" not in sent["cookies"] and "\n" not in sent["cookies"]
    assert "\r" not in sent["referrer"] and "\n" not in sent["referrer"]


def test_download_fails_when_no_primary_accepts(tmp_path, monkeypatch):
    """Cove closed, queue not ready, add rejected, socket timed out - all the
    same answer, and the browser keeps its download."""
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(nm, "deliver_to_primary", _Delivery(accept=False))

    result = handle_message(
        {"action": "download", "url": "https://example.invalid/file.zip"},
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert result["status"] == "error"


def test_download_writes_no_durable_request_when_the_primary_is_absent(
    tmp_path, monkeypatch
):
    """The bug: a file left behind here is picked up by the next launch."""
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(nm, "deliver_to_primary", _Delivery(accept=False))

    handle_message(
        {"action": "download", "url": "https://example.invalid/file.zip"},
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert list(tmp_path.rglob("*")) == []


def test_download_writes_no_durable_request_even_when_accepted(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(nm, "deliver_to_primary", _Delivery(accept=True))

    handle_message(
        {"action": "download", "url": "https://example.invalid/file.zip"},
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert list(tmp_path.rglob("*")) == []


def test_download_does_not_deliver_an_invalid_url(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    delivery = _Delivery(accept=True)
    monkeypatch.setattr(nm, "deliver_to_primary", delivery)

    result = handle_message(
        {"action": "download", "url": "file:///etc/passwd"},
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert result["status"] == "error"
    assert delivery.calls == []


def test_download_error_never_echoes_the_url_or_cookies(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(nm, "deliver_to_primary", _Delivery(accept=False))

    for msg in (
        {
            "action": "download",
            "url": "https://example.invalid/?token=dummysecrettoken",
            "cookies": "sid=dummysecretcookie",
        },
        {"action": "download", "url": "javascript:dummysecrettoken"},
        {"action": "download"},
    ):
        blob = json.dumps(handle_message(msg, rpc=MagicMock(), settings=MagicMock()))
        assert "dummysecrettoken" not in blob
        assert "dummysecretcookie" not in blob


def test_download_delivery_failure_is_reported_as_error(tmp_path, monkeypatch):
    """A crash inside delivery must not become a false 'ok'."""
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)

    def boom(request):
        raise RuntimeError("dummy transport failure")

    monkeypatch.setattr(nm, "deliver_to_primary", boom)
    result = handle_message(
        {"action": "download", "url": "https://example.invalid/file.zip"},
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert result["status"] == "error"
    assert list(tmp_path.rglob("*")) == []


# ---------------------------------------------------------------------------
# Diagnostics for the native messaging host.
#
# The host runs as a separate short-lived process with no GUI, so it keeps its
# own logger and its own file. stdout belongs to the native messaging protocol
# and must stay free of diagnostic text.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

from cove import diagnostics  # noqa: E402

RD_SHARE = "https://real-debrid.com/d/A7QK3ZP9WVN2XLMDR4TJ6YB8C5FGH1SE"
COOKIE = "session=eyJhbGciOiJIUzI1NiJ9.QWxhZGRpbjpvcGVuIHNlc2FtZQ"


@pytest.fixture
def host_log(tmp_path, monkeypatch):
    log = diagnostics.init_native_host_logger(tmp_path)
    monkeypatch.setattr(nm, "_LOG", log)
    yield log
    log.close()


def _events(log, event=None):
    return [r for r in log.records() if event is None or r["event"] == event]


def test_native_messaging_module_imports_without_qt():
    """PySide6 must stay out of the import path: the host is spawned by the
    browser for every message, including plain pings."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, cove.native_messaging; "
         "print('PySide6' in sys.modules or 'PySide6.QtCore' in sys.modules)"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "False"


def test_host_start_is_logged_with_versions(host_log):
    nm.log_host_start()
    start = _events(host_log, "host_start")
    assert len(start) == 1
    assert start[0]["component"] == "native_host"
    assert start[0]["fields"]["app_version"]


def test_host_logger_writes_to_its_own_file(tmp_path):
    log = diagnostics.init_native_host_logger(tmp_path)
    try:
        log.emit("native_host", "host_start", "INFO")
        assert (tmp_path / "logs" / diagnostics.NATIVE_LOG_NAME).exists()
        assert not (tmp_path / "logs" / diagnostics.APP_LOG_NAME).exists()
    finally:
        log.close()


def test_request_action_is_logged_without_the_payload(host_log, monkeypatch):
    monkeypatch.setattr(nm, "deliver_to_primary", lambda req: True)
    handle_message(
        {"action": "download", "url": RD_SHARE, "cookies": COOKIE,
         "filename": "video.mp4"},
        None, None,
    )
    received = _events(host_log, "request_received")
    assert len(received) == 1
    assert received[0]["fields"]["action"] == "download"
    dumped = json.dumps(host_log.records())
    assert "A7QK3ZP9WVN2XLMDR4TJ6YB8C5FGH1SE" not in dumped
    assert "eyJhbGciOiJIUzI1NiJ9" not in dumped
    assert "real-debrid.com/d/" not in dumped
    assert "video.mp4" not in dumped


def test_optional_request_id_is_recorded(host_log, monkeypatch):
    monkeypatch.setattr(nm, "deliver_to_primary", lambda req: True)
    handle_message(
        {"action": "download", "url": "https://example.com/a.mp4",
         "requestId": "51c2a711"},
        None, None,
    )
    assert _events(host_log, "request_received")[0]["request"] == "51c2a711"


def test_request_id_is_forwarded_to_the_primary(monkeypatch):
    seen = {}
    monkeypatch.setattr(nm, "deliver_to_primary",
                        lambda req: seen.update(req) or True)
    handle_message(
        {"action": "download", "url": "https://example.com/a.mp4",
         "requestId": "51c2a711"},
        None, None,
    )
    assert seen["request_id"] == "51c2a711"


def test_a_message_without_a_request_id_still_works(monkeypatch):
    seen = {}
    monkeypatch.setattr(nm, "deliver_to_primary",
                        lambda req: seen.update(req) or True)
    result = handle_message(
        {"action": "download", "url": "https://example.com/a.mp4"}, None, None
    )
    assert result["status"] == "ok"
    assert seen["request_id"] is None


@pytest.mark.parametrize(
    "value", ["bad id!", "x" * 200, 12345, "", None, "semi;colon"]
)
def test_invalid_request_ids_are_dropped_not_forwarded(value, monkeypatch):
    seen = {}
    monkeypatch.setattr(nm, "deliver_to_primary",
                        lambda req: seen.update(req) or True)
    handle_message(
        {"action": "download", "url": "https://example.com/a.mp4",
         "requestId": value},
        None, None,
    )
    assert seen["request_id"] is None


def test_ipc_attempt_and_result_are_logged(host_log, monkeypatch):
    monkeypatch.setattr(nm, "_send_to_primary",
                        lambda request, on_reason: (on_reason("ok"), True)[1])
    handle_message({"action": "download", "url": "https://example.com/a.mp4"},
                   None, None)
    assert _events(host_log, "ipc_attempt")
    result = _events(host_log, "ipc_result")
    assert result[0]["fields"]["result"] == "ok"


@pytest.mark.parametrize(
    "reason, message",
    [
        ("connect_timeout", "Cove is not available"),
        ("ack_timeout", "Cove is not available"),
        ("transport_error", "Cove is not available"),
        # Cove answered and said no; claiming it is not available would be a lie.
        ("rejected", "Cove refused this download."),
    ],
)
def test_ipc_failure_reasons_are_distinguished(reason, message, host_log, monkeypatch):
    monkeypatch.setattr(nm, "_send_to_primary",
                        lambda request, on_reason: (on_reason(reason), False)[1])
    response = handle_message(
        {"action": "download", "url": "https://example.com/a.mp4"}, None, None
    )
    assert response["message"] == message
    assert _events(host_log, "ipc_result")[0]["fields"]["result"] == reason


def test_gui_unavailable_is_recorded_when_the_socket_cannot_be_reached(
    host_log, monkeypatch
):
    monkeypatch.setattr(
        nm, "_send_to_primary",
        lambda request, on_reason: (_ for _ in ()).throw(RuntimeError("no gui")),
    )
    response = handle_message(
        {"action": "download", "url": "https://example.com/a.mp4"}, None, None
    )
    assert response["status"] == "error"
    assert _events(host_log, "ipc_result")[0]["fields"]["result"] == "app_unavailable"


def test_broad_exception_handlers_are_no_longer_silent(host_log, monkeypatch):
    monkeypatch.setattr(
        nm, "notify_primary_extension_seen",
        lambda: (_ for _ in ()).throw(RuntimeError("ping exploded")),
    )
    assert handle_message({"action": "ping"}, None, None)["status"] == "ok"
    assert _events(host_log, "ping_notify_failed")


def test_reply_status_is_logged(host_log, monkeypatch):
    monkeypatch.setattr(nm, "deliver_to_primary", lambda req: False)
    handle_message({"action": "download", "url": "https://example.com/a.mp4"},
                   None, None)
    reply = _events(host_log, "reply_sent")
    assert reply[0]["fields"]["status"] == "error"


def test_stdout_carries_only_protocol_frames(tmp_path, monkeypatch, capsys):
    """Anything written to stdout that is not a length-prefixed frame breaks
    the browser's parser, so diagnostics must never go there."""
    log = diagnostics.init_native_host_logger(tmp_path)
    monkeypatch.setattr(nm, "_LOG", log)
    monkeypatch.setattr(nm, "deliver_to_primary", lambda req: True)

    payload = encode_message({"action": "download", "url": "https://example.com/a.mp4"})
    stdin = io.BytesIO(payload)
    stdout = io.BytesIO()
    monkeypatch.setattr(nm, "_binary_stdio", lambda: (stdin, stdout))
    monkeypatch.setattr(nm.Settings, "load", staticmethod(lambda: None))
    nm.main()

    raw = stdout.getvalue()
    length = struct.unpack("<I", raw[:4])[0]
    assert json.loads(raw[4:4 + length])["status"] == "ok"
    assert len(raw) == 4 + length, "no extra bytes on stdout"
    assert capsys.readouterr().out == ""
    log.close()


def test_session_end_is_logged(tmp_path, monkeypatch):
    log = diagnostics.init_native_host_logger(tmp_path)
    monkeypatch.setattr(nm, "_LOG", log)
    monkeypatch.setattr(nm, "_binary_stdio", lambda: (io.BytesIO(b""), io.BytesIO()))
    monkeypatch.setattr(nm.Settings, "load", staticmethod(lambda: None))
    nm.main()
    assert [r["event"] for r in log.records()][-1] == "host_stop"
    log.close()


def test_host_diagnostics_failure_never_breaks_a_reply(monkeypatch):
    monkeypatch.setattr(nm, "_LOG", None)
    monkeypatch.setattr(nm, "deliver_to_primary", lambda req: True)
    assert handle_message(
        {"action": "download", "url": "https://example.com/a.mp4"}, None, None
    )["status"] == "ok"


# ---- request-id propagation across the IPC boundary ------------------------

from cove import single_instance as si  # noqa: E402


def _browser_message(**extra):
    message = {"version": si.PROTOCOL_VERSION, "action": si.BROWSER_DOWNLOAD_ACTION,
               "url": "https://example.com/a.mp4"}
    message.update(extra)
    return message


def test_validate_browser_download_accepts_an_optional_request_id():
    request = si.validate_browser_download(_browser_message(request_id="51c2a711"))
    assert request["request_id"] == "51c2a711"


def test_validate_browser_download_without_a_request_id_still_validates():
    request = si.validate_browser_download(_browser_message())
    assert request["request_id"] is None
    assert request["url"] == "https://example.com/a.mp4"


@pytest.mark.parametrize("value", ["bad id!", "x" * 200, 5, ""])
def test_validate_browser_download_drops_an_invalid_request_id(value):
    request = si.validate_browser_download(_browser_message(request_id=value))
    assert request["request_id"] is None


def test_an_unknown_extra_key_does_not_reject_a_browser_download():
    """Forward compatibility: a newer extension may add keys this build has
    never heard of, and the download must still be accepted."""
    request = si.validate_browser_download(_browser_message(somethingNew="x"))
    assert request["url"] == "https://example.com/a.mp4"


def test_browser_download_add_is_tagged_as_extension_intake(tmp_path):
    from cove import app as cove_app

    diagnostics.shutdown_logger()
    log = diagnostics.init_app_logger(tmp_path)
    try:
        recorded = {}

        class _Queue:
            def add_url(self, url, **kw):
                recorded.update(kw)
                return 7

        gate = cove_app.BrowserDownloadGate()
        gate.queue = _Queue()
        gate.ready = True
        assert gate.accept(
            {"url": "https://example.com/a.mp4", "request_id": "51c2a711"}
        ) is True
        assert recorded["intake"] == "extension"
        results = [r for r in log.records() if r["event"] == "gui_result"]
        assert results[0]["request"] == "51c2a711"
        assert results[0]["component"] == "extension.native_bridge"
        assert results[0]["task"] == 7
    finally:
        diagnostics.shutdown_logger()


def test_browser_download_rejection_is_logged_without_the_url(tmp_path):
    from cove import app as cove_app

    diagnostics.shutdown_logger()
    log = diagnostics.init_app_logger(tmp_path)
    try:
        gate = cove_app.BrowserDownloadGate()
        gate.ready = False
        assert gate.accept({"url": RD_SHARE, "request_id": "51c2a711"}) is False
        results = [r for r in log.records() if r["event"] == "gui_result"]
        assert results[0]["fields"]["result"] == "not_ready"
        assert "A7QK3ZP9WVN2XLMDR4TJ6YB8C5FGH1SE" not in json.dumps(log.records())
    finally:
        diagnostics.shutdown_logger()


# ---------------------------------------------------------------------------
# Phase 8: the request id survives every hop
# ---------------------------------------------------------------------------


def test_one_request_id_spans_extension_host_and_gui(tmp_path, monkeypatch):
    """content script -> background -> native message -> host -> IPC -> queue.

    The extension side of the chain is covered by the Node tests; this walks
    the message the background produced through the host, the IPC validator
    and the GUI gate, and proves the same id lands on the queue-side record.
    """
    from cove import app as cove_app

    diagnostics.shutdown_logger()
    app_log = diagnostics.init_app_logger(tmp_path / "gui")
    host_log = diagnostics.init_native_host_logger(tmp_path / "host")
    monkeypatch.setattr(nm, "_LOG", host_log)

    forwarded = {}
    monkeypatch.setattr(nm, "deliver_to_primary",
                        lambda req: forwarded.update(req) or True)
    try:
        # The frame the background actually sends.
        nm.handle_message(
            {"action": "download", "url": "https://example.com/a.mp4",
             "requestId": "51c2a711"},
            None, None,
        )
        assert forwarded["request_id"] == "51c2a711"

        # The IPC frame the host builds from it.
        message = {"version": si.PROTOCOL_VERSION,
                   "action": si.BROWSER_DOWNLOAD_ACTION,
                   "url": forwarded["url"], "request_id": forwarded["request_id"]}
        validated = si.validate_browser_download(message)
        assert validated["request_id"] == "51c2a711"

        # The GUI gate that finally adds it.
        class _Queue:
            def add_url(self, url, **kw):
                return 11

        gate = cove_app.BrowserDownloadGate()
        gate.queue = _Queue()
        gate.ready = True
        assert gate.accept(validated) is True

        assert [r["request"] for r in host_log.records()
                if r["event"] == "request_received"] == ["51c2a711"]
        gui = [r for r in app_log.records() if r["event"] == "gui_result"]
        assert gui[0]["request"] == "51c2a711"
        assert gui[0]["task"] == 11
    finally:
        host_log.close()
        diagnostics.shutdown_logger()


def test_an_old_extension_without_a_request_id_still_completes(tmp_path, monkeypatch):
    from cove import app as cove_app

    diagnostics.shutdown_logger()
    app_log = diagnostics.init_app_logger(tmp_path / "gui")
    host_log = diagnostics.init_native_host_logger(tmp_path / "host")
    monkeypatch.setattr(nm, "_LOG", host_log)

    forwarded = {}
    monkeypatch.setattr(nm, "deliver_to_primary",
                        lambda req: forwarded.update(req) or True)
    try:
        response = nm.handle_message(
            {"action": "download", "url": "https://example.com/a.mp4"}, None, None
        )
        assert response["status"] == "ok"
        assert forwarded["request_id"] is None

        validated = si.validate_browser_download({
            "version": si.PROTOCOL_VERSION,
            "action": si.BROWSER_DOWNLOAD_ACTION,
            "url": forwarded["url"],
        })

        class _Queue:
            def add_url(self, url, **kw):
                return 12

        gate = cove_app.BrowserDownloadGate()
        gate.queue = _Queue()
        gate.ready = True
        assert gate.accept(validated) is True
        gui = [r for r in app_log.records() if r["event"] == "gui_result"]
        assert gui[0].get("request") is None
    finally:
        host_log.close()
        diagnostics.shutdown_logger()


def test_an_old_host_that_omits_the_request_id_is_still_accepted():
    """Forward compatibility in the other direction: a primary running a newer
    build must not require a key an older host never sends."""
    validated = si.validate_browser_download({
        "version": si.PROTOCOL_VERSION,
        "action": si.BROWSER_DOWNLOAD_ACTION,
        "url": "https://example.com/a.mp4",
        "cookies": "a=b",
    })
    assert validated["url"] == "https://example.com/a.mp4"
    assert validated["request_id"] is None


# ---- Oversized cookie jars -------------------------------------------------
#
# An older installed extension joins the whole browser cookie jar into one
# header. The GUI bounds that field, so the *entire* download was rejected for
# it. Dropping the header keeps an otherwise valid download; the host mirrors
# the rule so an extension that has not updated yet still works.


def _cookie_limit() -> int:
    from cove.single_instance import MAX_BROWSER_COOKIES_LENGTH

    return MAX_BROWSER_COOKIES_LENGTH


def test_the_host_drops_a_cookie_header_the_gui_would_reject(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    delivery = _Delivery(accept=True)
    monkeypatch.setattr(nm, "deliver_to_primary", delivery)

    result = handle_message(
        {
            "action": "download",
            "url": "https://example.invalid/file.zip",
            "cookies": "c" * (_cookie_limit() + 1),
        },
        rpc=MagicMock(),
        settings=MagicMock(),
    )

    assert result["status"] == "ok"
    assert delivery.calls[0]["cookies"] == ""
    assert delivery.calls[0]["url"] == "https://example.invalid/file.zip"


def test_the_host_never_truncates_a_cookie_header(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    delivery = _Delivery(accept=True)
    monkeypatch.setattr(nm, "deliver_to_primary", delivery)

    handle_message(
        {
            "action": "download",
            "url": "https://example.invalid/file.zip",
            "cookies": "sid=dummysecretcookie" + "c" * _cookie_limit(),
        },
        rpc=MagicMock(),
        settings=MagicMock(),
    )

    assert delivery.calls[0]["cookies"] == ""


def test_a_cookie_header_within_the_limit_is_forwarded_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    delivery = _Delivery(accept=True)
    monkeypatch.setattr(nm, "deliver_to_primary", delivery)
    cookies = "c" * _cookie_limit()

    handle_message(
        {
            "action": "download",
            "url": "https://example.invalid/file.zip",
            "cookies": cookies,
        },
        rpc=MagicMock(),
        settings=MagicMock(),
    )

    assert delivery.calls[0]["cookies"] == cookies


def test_the_host_cookie_bound_is_the_gui_cookie_bound():
    assert nm.max_browser_cookies_length() == _cookie_limit()


def test_dropping_cookies_is_logged_without_any_cookie_content(
    host_log, tmp_path, monkeypatch
):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    monkeypatch.setattr(nm, "deliver_to_primary", _Delivery(accept=True))

    handle_message(
        {
            "action": "download",
            "url": "https://example.invalid/dummysecretpath.zip",
            "cookies": "sid=dummysecretcookie" + "c" * _cookie_limit(),
        },
        rpc=MagicMock(),
        settings=MagicMock(),
    )

    dropped = _events(host_log, "cookies_dropped")
    assert dropped
    dumped = json.dumps(host_log.records())
    assert "dummysecretcookie" not in dumped
    assert "dummysecretpath" not in dumped


def test_an_explicit_gui_rejection_is_not_reported_as_cove_missing(
    host_log, monkeypatch
):
    monkeypatch.setattr(
        nm, "_send_to_primary",
        lambda request, on_reason: (on_reason("oversized_message"), False)[1],
    )
    response = handle_message(
        {"action": "download", "url": "https://example.com/a.mp4"}, None, None
    )
    assert response["status"] == "error"
    assert response["message"] != "Cove is not available"
    assert _events(host_log, "ipc_result")[0]["fields"]["result"] == "oversized_message"
    assert "https://example.com/a.mp4" not in json.dumps(host_log.records())


def test_a_transport_failure_still_reports_cove_as_unavailable(host_log, monkeypatch):
    monkeypatch.setattr(
        nm, "_send_to_primary",
        lambda request, on_reason: (on_reason("connect_timeout"), False)[1],
    )
    response = handle_message(
        {"action": "download", "url": "https://example.com/a.mp4"}, None, None
    )
    assert response["message"] == "Cove is not available"


def test_ping_is_unaffected_by_the_rejection_categories(host_log, monkeypatch):
    monkeypatch.setattr(nm, "notify_primary_extension_seen", lambda: False)
    response = handle_message({"action": "ping"}, None, None)
    assert response["status"] == "ok"
    assert "category" not in response
