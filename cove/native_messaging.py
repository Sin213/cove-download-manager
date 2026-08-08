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
from urllib.parse import urlsplit

from . import __version__, diagnostics
from .aria2 import Aria2RPC, Aria2Error
from .config import Settings

MAX_MESSAGE_SIZE = 1024 * 1024  # 1 MB

# Reply sentences for a handoff the running Cove refused outright. Most
# failures really are "no Cove answered", which is what the default says; a
# request Cove explicitly rejected must not be reported as Cove being closed.
_HANDOFF_UNAVAILABLE = "Cove is not available"
_HANDOFF_SENTENCES = {
    "oversized_message": "Cove refused this request: it is too large.",
    "truncated_message": "Cove refused this request: it was incomplete.",
    "malformed_json": "Cove refused this request: it could not be read.",
    "invalid_schema": "Cove refused this request: unexpected format.",
    "invalid_url": "Cove refused this request: unsupported link.",
    "too_many_urls": "Cove refused this request: too many links.",
    "unknown_action": "Cove refused this request: unsupported action.",
    "unsupported_version": "Cove refused this request: please update Cove.",
    "rejected": "Cove refused this download.",
    "gui_rejected": "Cove refused this download.",
}

# The reason the last handoff attempt ended, as a fixed category. The host
# loop handles exactly one message at a time, so a plain module attribute is
# enough - and it keeps deliver_to_primary's single-argument contract, which
# both the tests and any external caller already rely on.
_LAST_HANDOFF_REASON = None

# This host's own logger, initialised in main(). It writes to its own file:
# the GUI is a different process and two writers must never share one file.
# stdout is reserved for protocol frames, so nothing here may print.
_LOG = None


def _log(event: str, level: str = "INFO", request_id=None, exc=None, **fields) -> None:
    log = _LOG
    if log is None:
        return
    try:
        log.emit("native_host", event, level, request_id=request_id, exc=exc, **fields)
    except Exception:
        pass


def log_host_start() -> None:
    _log("host_start", "INFO", app_version=__version__,
         mode=diagnostics.install_mode())


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


def max_browser_cookies_length() -> int:
    """The GUI's own cookie bound, read from the one place that defines it.

    Imported lazily because single_instance pulls in Qt networking and a ping
    must stay cheap. If that import fails the handoff cannot happen at all, so
    the safe answer is to drop cookies rather than invent a second bound here.
    """
    try:
        from .single_instance import MAX_BROWSER_COOKIES_LENGTH

        return MAX_BROWSER_COOKIES_LENGTH
    except Exception:
        return 0


def validate_url(url: str) -> bool:
    """Whether a handed-over URL may cross into the queue.

    A scheme-prefix match was not enough: `https:///file` and `ftp:///path`
    passed it while naming no host at all, so a malformed value from an
    extension or any other client reached queue and network handling as an
    untrusted string. Network schemes must carry a usable authority.
    """
    if not url or not isinstance(url, str):
        return False
    candidate = url.strip()
    if not _is_control_free(candidate):
        return False
    if not candidate.lower().startswith(("http://", "https://", "ftp://")):
        return False
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return False
    try:
        # Rejects an invalid or out-of-range port; `hostname` lowercases and
        # strips the IPv6 brackets and any userinfo for us.
        if parts.port is not None and not (0 < parts.port < 65536):
            return False
    except ValueError:
        return False
    return bool(parts.hostname)


def _is_control_free(value: str) -> bool:
    return all(ord(ch) >= 32 and ch != "\x7f" for ch in value)


def _send_to_primary(request: dict, on_reason) -> bool:
    """Do the actual IPC round trip, reporting a safe failure class.

    Split out from deliver_to_primary so the reason a handoff failed can be
    recorded without changing what deliver_to_primary returns or when.
    """
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
    return send_browser_download(server_name(DATA_DIR), request, on_reason=on_reason)


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
    global _LAST_HANDOFF_REASON

    request_id = request.get("request_id")
    reasons = []
    _log("ipc_attempt", "INFO", request_id=request_id)
    try:
        accepted = _send_to_primary(request, reasons.append)
    except Exception as exc:
        # Qt missing, no socket, no running GUI: all of these look the same
        # from here, so report the class rather than guessing.
        _LAST_HANDOFF_REASON = "app_unavailable"
        _log("ipc_result", "WARNING", request_id=request_id,
             result="app_unavailable", exc=exc)
        return False
    reason = reasons[-1] if reasons else ("ok" if accepted else "unknown")
    _LAST_HANDOFF_REASON = reason
    _log("ipc_result", "INFO" if accepted else "WARNING", request_id=request_id,
         result=reason, accepted=bool(accepted))
    return accepted


def notify_primary_extension_seen() -> bool:
    """Tell a running Cove that the extension just made contact.

    Best effort and never authoritative: the ping reply below is this host's
    own answer, so a closed Cove (or any IPC failure) must not turn a healthy
    ping into an error. Imported lazily for the same reason as
    deliver_to_primary - a ping should stay cheap.
    """
    try:
        from PySide6.QtCore import QCoreApplication

        from .config import DATA_DIR
        from .single_instance import send_extension_ping, server_name

        if QCoreApplication.instance() is None:
            QCoreApplication([])
        return send_extension_ping(server_name(DATA_DIR))
    except Exception:
        return False


def handle_message(
    msg: dict,
    rpc: Aria2RPC | None,
    settings: Settings | None,
) -> dict:
    action = msg.get("action", "")
    # Optional, additive and backward compatible: an older extension simply
    # omits it, and an older host ignores it.
    request_id = diagnostics.normalize_request_id(msg.get("requestId"))
    _log("request_received", "INFO", request_id=request_id,
         action=action if isinstance(action, str) else "invalid")

    def reply(response: dict) -> dict:
        _log("reply_sent", "INFO", request_id=request_id,
             action=action if isinstance(action, str) else "invalid",
             status=response.get("status"))
        return response

    if action == "ping":
        # The extension's own heartbeat doubles as the GUI's "extension is
        # installed and talking" signal. Failure here changes nothing.
        try:
            notify_primary_extension_seen()
        except Exception as exc:
            _log("ping_notify_failed", "WARNING", request_id=request_id, exc=exc)
        return reply({"status": "ok", "version": __version__})

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
            return reply({"status": "error", "message": "Invalid or blocked URL"})

        requested_dir = msg.get("directory")
        cookies = _sanitize_header(msg.get("cookies", ""))
        if len(cookies) > max_browser_cookies_length():
            # An extension older than this host still joins the browser's whole
            # cookie jar into one header. The GUI bounds that field and would
            # refuse the entire request for it, so drop the header instead and
            # let the download through. Never truncated: a partial cookie
            # header authenticates nothing and would fail downstream anyway.
            _log("cookies_dropped", "INFO", request_id=request_id,
                 reason="over_gui_limit")
            cookies = ""
        request = {
            "url": url,
            "filename": msg.get("filename") or None,
            "directory": requested_dir if isinstance(requested_dir, str) else None,
            "cookies": cookies,
            "referrer": _sanitize_header(msg.get("referrer", "")),
            "user_agent": _sanitize_header(msg.get("userAgent", "")),
            "file_size": msg.get("fileSize") if isinstance(msg.get("fileSize"), int) else 0,
            # Additive and optional. Old primaries ignore an unknown key.
            "request_id": request_id,
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
        global _LAST_HANDOFF_REASON
        _LAST_HANDOFF_REASON = None
        try:
            accepted = deliver_to_primary(request)
        except Exception:
            accepted = False
        if not accepted:
            # Fixed sentences only, chosen by the fixed category the primary
            # reported: never the URL, cookies or referrer, which the extension
            # logs to the browser console.
            return reply({
                "status": "error",
                "message": _HANDOFF_SENTENCES.get(
                    _LAST_HANDOFF_REASON, _HANDOFF_UNAVAILABLE
                ),
            })
        return reply({"status": "ok", "message": "Download queued in Cove"})

    if action == "status":
        if rpc is None:
            return reply({"status": "error", "message": "Cove is not configured"})
        try:
            active = rpc.tell_active()
            return reply({"status": "ok", "downloads": active})
        except Aria2Error as e:
            _log("status_failed", "WARNING", request_id=request_id, exc=e)
            return reply({"status": "error", "message": str(e)})

    return reply({"status": "error", "message": f"Unknown action: {action!r}"})


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
    global _LOG
    if _LOG is None:
        try:
            from .config import DATA_DIR

            _LOG = diagnostics.init_native_host_logger(DATA_DIR)
        except Exception:
            _LOG = None
    log_host_start()

    try:
        settings = Settings.load()
        rpc = Aria2RPC(settings)
    except Exception as e:
        _log("settings_unavailable", "WARNING", exc=e)
        settings = None
        rpc = None

    stdin, stdout = _binary_stdio()

    while True:
        msg = decode_message(stdin)
        if msg is None:
            break
        try:
            response = handle_message(msg, rpc, settings)
        except Exception as exc:
            # A crash here kills the host and the browser respawns it,
            # silently dropping the message (and risking a crash loop).
            _log("handler_crashed", "ERROR", exc=exc)
            response = {"status": "error", "message": "Internal error handling message"}
        stdout.write(encode_message(response))
        stdout.flush()

    _log("host_stop", "INFO")


if __name__ == "__main__":
    main()
