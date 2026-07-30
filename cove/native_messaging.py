"""Native messaging host for Firefox extension integration.

Communicates via stdin/stdout using the WebExtension native messaging
protocol (4-byte little-endian length prefix + JSON body). Reads Cove's
settings to connect to aria2 RPC and queue downloads.

Usage:
    python -m cove.native_messaging
"""
from __future__ import annotations

import io
import json
import os
import struct
import sys
from typing import Any

from . import __version__
from .aria2 import Aria2RPC, Aria2Error
from .config import Settings

MAX_MESSAGE_SIZE = 1024 * 1024  # 1 MB


def _read_exact(stream: io.BufferedIOBase, n: int) -> bytes | None:
    """Read exactly n bytes, looping over short reads. None on EOF.

    A single ``stream.read(n)`` on a pipe may return fewer than n bytes, so
    a one-shot read can spuriously truncate a large but valid message.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def decode_message(stream: io.BufferedIOBase) -> dict | None:
    raw_length = _read_exact(stream, 4)
    if raw_length is None:
        return None
    length = struct.unpack("<I", raw_length)[0]
    if length == 0 or length > MAX_MESSAGE_SIZE:
        return None
    data = _read_exact(stream, length)
    if data is None:
        return None
    try:
        decoded = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        # Malformed frame: stop rather than crashing the host loop. The
        # browser will respawn the host on the next message.
        return None
    # Valid JSON that isn't an object would crash handle_message; treat it
    # like a malformed frame.
    return decoded if isinstance(decoded, dict) else None


def encode_message(msg: dict) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def _sanitize_header(value: Any) -> str:
    """Strip CR/LF so an extension-supplied value can't inject extra
    headers into the request aria2 makes (header/CRLF injection)."""
    if not isinstance(value, str):
        return ""
    return value.replace("\r", "").replace("\n", "")


def validate_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    lower = url.lower().strip()
    if lower.startswith(("http://", "https://", "ftp://")):
        return True
    return False


def deliver_to_primary(request: dict) -> bool:
    """Hand one browser download to the Cove process running right now.

    Returns True only if a live primary validated the request and its
    QueueManager accepted it, which is the only condition under which the
    extension may cancel the browser's own transfer. No primary listening, a
    primary whose queue isn't ready yet, a rejected add, a shutting-down
    primary, or a socket timeout all return False - and nothing is written
    anywhere, so a later Cove launch cannot inherit the request.

    Imported lazily so the Qt local-socket machinery is only pulled in when a
    download is actually being forwarded (ping/status stay cheap).
    """
    try:
        from PySide6.QtCore import QCoreApplication

        from .config import DATA_DIR
        from .single_instance import send_browser_download, server_name

        # QLocalSocket needs a QCoreApplication for its event dispatcher. This
        # host is a short-lived console process with no Qt app of its own, so
        # create a minimal one; it is never exec()'d, and no GUI is possible
        # (QCoreApplication, not QApplication) - the browser respawns this
        # host freely, so accidentally opening a window here would loop.
        if QCoreApplication.instance() is None:
            QCoreApplication([])
        return send_browser_download(server_name(DATA_DIR), request)
    except Exception:
        return False


def handle_message(
    msg: dict,
    rpc: Aria2RPC | None,
    settings: Settings | None,
) -> dict:
    action = msg.get("action", "")

    if action == "ping":
        return {"status": "ok", "version": __version__}

    if action == "download":
        url = msg.get("url", "")
        # Normalize once so the same URL that passed validation is the one
        # forwarded to the drop file (validation checks a stripped copy, so
        # an untrimmed original could slip through otherwise).
        if isinstance(url, str):
            url = url.strip()
        if not validate_url(url):
            # Never echo the URL: the extension writes this message to the
            # browser console, and the URL may carry a session token.
            return {"status": "error", "message": "Invalid or blocked URL"}

        requested_dir = msg.get("directory")
        request = {
            "url": url,
            "filename": msg.get("filename") or None,
            "directory": requested_dir if isinstance(requested_dir, str) else None,
            "cookies": _sanitize_header(msg.get("cookies", "")),
            "referrer": _sanitize_header(msg.get("referrer", "")),
            "user_agent": _sanitize_header(msg.get("userAgent", "")),
            "file_size": msg.get("fileSize") if isinstance(msg.get("fileSize"), int) else 0,
        }

        # Every download (plain, HLS, or extractor) is handed to the *running*
        # Cove GUI process, which calls QueueManager.add_url() itself. This
        # host is a separate process with no access to the live QueueManager,
        # so an rpc.add_uri() shortcut here would add the raw URL straight to
        # aria2, bypassing debrid resolution (Real-Debrid/AllDebrid/TorBox),
        # category routing, and Cove's own DB row.
        #
        # Crucially the handoff is synchronous and nothing is persisted. The
        # extension cancels the browser's own download the moment it sees
        # {"status": "ok"}, so "ok" may only be returned once a currently
        # running Cove has actually accepted this exact request. The previous
        # implementation wrote a durable drop file and answered "ok" whether
        # or not any Cove process existed; the file was then consumed at the
        # next launch, which is what made a download intercepted while Cove
        # was closed reappear later.
        try:
            accepted = deliver_to_primary(request)
        except Exception:
            accepted = False
        if not accepted:
            # Fixed sentence: never the URL, cookies or referrer, which the
            # extension logs to the browser console.
            return {"status": "error", "message": "Cove is not available"}
        return {"status": "ok", "message": "Download queued in Cove"}

    if action == "status":
        if rpc is None:
            return {"status": "error", "message": "Cove is not configured"}
        try:
            active = rpc.tell_active()
            return {"status": "ok", "downloads": active}
        except Aria2Error as e:
            return {"status": "error", "message": str(e)}

    return {"status": "error", "message": f"Unknown action: {action!r}"}


def _binary_stdio() -> tuple[io.BufferedReader, io.BufferedWriter]:
    """Return binary (stdin, stdout) streams for the native messaging pipe.

    A console/dev launch exposes ``sys.stdin.buffer`` directly. But the
    packaged Windows app is a GUI-subsystem (``--windowed``) build, where
    ``sys.stdin``/``sys.stdout`` are ``None`` even though Firefox passed
    real pipe handles. In that case reconstruct binary streams from the
    inherited OS std handles, otherwise the protocol can't be read at all.
    """
    stdin = getattr(sys.stdin, "buffer", None)
    stdout = getattr(sys.stdout, "buffer", None)
    if stdin is not None and stdout is not None:
        return stdin, stdout

    if sys.platform == "win32":
        import msvcrt
        from ctypes import windll, wintypes

        STD_INPUT_HANDLE = -10
        STD_OUTPUT_HANDLE = -11
        # A HANDLE is pointer-sized; without an explicit restype ctypes
        # defaults to c_int (32-bit) and truncates the handle on 64-bit
        # builds, which can yield an invalid fd and break the pipe.
        get_std_handle = windll.kernel32.GetStdHandle
        get_std_handle.argtypes = [wintypes.DWORD]
        get_std_handle.restype = wintypes.HANDLE
        INVALID_HANDLE = wintypes.HANDLE(-1).value
        h_in = get_std_handle(STD_INPUT_HANDLE)
        h_out = get_std_handle(STD_OUTPUT_HANDLE)
        if h_in in (INVALID_HANDLE, 0) or h_out in (INVALID_HANDLE, 0):
            raise OSError("stdin/stdout handles not available")
        fd_in = msvcrt.open_osfhandle(h_in, os.O_RDONLY | os.O_BINARY)
        fd_out = msvcrt.open_osfhandle(h_out, os.O_WRONLY | os.O_BINARY)
        return os.fdopen(fd_in, "rb"), os.fdopen(fd_out, "wb")

    return os.fdopen(0, "rb"), os.fdopen(1, "wb")


def main() -> None:
    try:
        settings = Settings.load()
        rpc = Aria2RPC(settings)
    except Exception as e:
        settings = None
        rpc = None

    stdin, stdout = _binary_stdio()

    while True:
        msg = decode_message(stdin)
        if msg is None:
            break
        try:
            response = handle_message(msg, rpc, settings)
        except Exception:
            # A crash here kills the host and the browser respawns it,
            # silently dropping the message (and risking a crash loop).
            response = {"status": "error", "message": "Internal error handling message"}
        stdout.write(encode_message(response))
        stdout.flush()


if __name__ == "__main__":
    main()
