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
