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


def test_download_does_not_inject_headers_via_crlf():
    rpc = MagicMock()
    rpc.add_uri.return_value = "gid-1"
    settings = MagicMock()
    settings.download_dir = "/tmp"
    settings.connections_per_server = 8
    msg = {
        "action": "download",
        "url": "https://example.com/f.zip",
        "cookies": "s=1\r\nX-Evil: 1",
        "referrer": "https://example.com/\r\nHost: evil",
    }
    handle_message(msg, rpc=rpc, settings=settings)
    headers = rpc.add_uri.call_args[1]["headers"]
    assert all("\r" not in h and "\n" not in h for h in headers)


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


def test_handle_download():
    mock_rpc = MagicMock()
    mock_rpc.add_uri.return_value = "gid-123"
    mock_settings = MagicMock()
    mock_settings.download_dir = "/tmp/downloads"
    mock_settings.connections_per_server = 16

    msg = {
        "action": "download",
        "url": "https://example.com/file.zip",
        "filename": "file.zip",
        "referrer": "https://example.com/page",
        "cookies": "session=abc",
    }
    result = handle_message(msg, rpc=mock_rpc, settings=mock_settings)
    assert result["status"] == "ok"
    assert result["gid"] == "gid-123"

    call_args = mock_rpc.add_uri.call_args
    assert call_args[0][0] == ["https://example.com/file.zip"]
    headers = call_args[1]["headers"]
    assert "Cookie: session=abc" in headers
    assert "Referer: https://example.com/page" in headers


def test_handle_youtube_download_queues_extractor(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    rpc = MagicMock()
    settings = MagicMock()
    settings.download_dir = "/tmp/downloads"

    result = handle_message(
        {
            "action": "download",
            "url": "https://www.youtube.com/watch?v=BMcJirSZACw",
            "filename": "The BEST Way To Cook Pork Tenderloin.mp4",
        },
        rpc=rpc,
        settings=settings,
    )

    assert result == {"status": "ok", "message": "Video download queued in Cove"}
    drop_files = list((tmp_path / "drop").glob("extractor-*.json"))
    assert len(drop_files) == 1
    assert '"url": "https://www.youtube.com/watch?v=BMcJirSZACw"' in drop_files[0].read_text()
    rpc.add_uri.assert_not_called()


def test_video_drop_payload_preserves_headers_and_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cove.config.DATA_DIR", tmp_path)
    result = handle_message(
        {
            "action": "download",
            "url": "https://www.youtube.com/watch?v=BMcJirSZACw",
            "filename": "clip.mp4",
            "directory": "/tmp/videos",
            "cookies": "session=abc",
            "referrer": "https://example.com/page",
            "userAgent": "TestUA/1.0",
        },
        rpc=MagicMock(),
        settings=MagicMock(),
    )
    assert result["status"] == "ok"
    drop_files = list((tmp_path / "drop").glob("extractor-*.json"))
    assert len(drop_files) == 1
    data = json.loads(drop_files[0].read_text())
    assert data["dir"] == "/tmp/videos"
    assert data["cookies"] == "session=abc"
    assert data["referrer"] == "https://example.com/page"
    assert data["userAgent"] == "TestUA/1.0"
    # Atomic write: no half-written .tmp files left behind.
    assert list((tmp_path / "drop").glob("*.tmp")) == []


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
