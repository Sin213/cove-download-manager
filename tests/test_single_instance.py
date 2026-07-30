"""Tests for local single-instance IPC: naming, framing/schema, and the
real client/server round trip over QLocalServer/QLocalSocket.

Client and server run in separate OS processes here (matching how Cove
actually uses this module - two independent `cove` launches). Running both
ends in one Python thread doesn't work: QLocalSocket's blocking waitFor*
calls only service that socket's own descriptor, so a same-thread client
would starve the server's newConnection/readyRead notifications.
"""
import json
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from cove.single_instance import (
    MAX_BROWSER_COOKIES_LENGTH,
    MAX_MESSAGE_BYTES,
    MAX_URLS_PER_MESSAGE,
    MessageError,
    SingleInstanceServer,
    decode_payload,
    encode_message,
    send_browser_download,
    send_to_primary,
    server_name,
    validate_message,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

QCoreApplication.instance() or QCoreApplication([])

MAGNET = "magnet:?xt=urn:btih:" + "a" * 40
MAGNET_2 = "magnet:?xt=urn:btih:" + "b" * 40


def _unique_name() -> str:
    return f"cove-test-{uuid.uuid4().hex[:16]}"


def _forward_in_subprocess(name: str, urls: list[str], timeout: float = 5.0) -> bool:
    """Run `send_to_primary` in a separate process while pumping this
    process's Qt event loop so the in-process server can respond."""
    app = QCoreApplication.instance()
    script = (
        "from cove.single_instance import send_to_primary\n"
        f"ok = send_to_primary({name!r}, {urls!r})\n"
        'print("OK" if ok else "FAIL")\n'
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    out, _ = proc.communicate(timeout=2)
    return out.strip() == "OK"


def _raw_ipc_roundtrip(name: str, message: dict, timeout: float = 5.0) -> dict:
    """Send an arbitrary (possibly invalid) framed request and return the
    parsed response. Bypasses send_to_primary()'s own construction of the
    request so tests can send combinations `send_to_primary` would never
    produce itself (e.g. `activate` with a non-empty `urls`)."""
    app = QCoreApplication.instance()
    script = (
        "import struct, json\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "from PySide6.QtNetwork import QLocalSocket\n"
        "app = QCoreApplication([])\n"
        "sock = QLocalSocket()\n"
        f"sock.connectToServer({name!r})\n"
        "assert sock.waitForConnected(2000)\n"
        f"msg = json.dumps({message!r}).encode()\n"
        'sock.write(struct.pack(">I", len(msg)) + msg)\n'
        "sock.flush()\n"
        "assert sock.waitForReadyRead(2000)\n"
        "buf = bytearray(bytes(sock.readAll()))\n"
        "while len(buf) < 4:\n"
        "    assert sock.waitForReadyRead(2000)\n"
        "    buf.extend(bytes(sock.readAll()))\n"
        "(length,) = struct.unpack('>I', bytes(buf[:4]))\n"
        "del buf[:4]\n"
        "while len(buf) < length:\n"
        "    assert sock.waitForReadyRead(2000)\n"
        "    buf.extend(bytes(sock.readAll()))\n"
        "resp = json.loads(bytes(buf[:length]))\n"
        "print(json.dumps(resp))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    out, err = proc.communicate(timeout=2)
    assert proc.returncode == 0, err
    return json.loads(out.strip())


def _pump_until(predicate, timeout: float = 5.0) -> bool:
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --- server_name --------------------------------------------------------

def test_server_name_deterministic_for_same_dir(tmp_path):
    assert server_name(tmp_path) == server_name(tmp_path)


def test_server_name_differs_for_different_dirs(tmp_path):
    a = tmp_path / "install-a"
    b = tmp_path / "install-b"
    assert server_name(a) != server_name(b)


def test_server_name_bounded(tmp_path):
    deep = tmp_path
    for i in range(40):
        deep = deep / f"segment-{i}"
    assert len(server_name(deep)) < 64


def test_server_name_contains_no_raw_path(tmp_path):
    name = server_name(tmp_path)
    assert str(tmp_path) not in name
    for part in tmp_path.parts:
        if part and part != "/":
            assert part not in name


def test_server_name_handles_portable_style_paths(tmp_path):
    portable = tmp_path / "cove-app-data" / "cove-download-manager"
    assert server_name(portable).startswith("cove-")


def test_server_name_handles_unicode_paths(tmp_path):
    unicode_dir = tmp_path / "ééé-日本語"
    name = server_name(unicode_dir)
    assert name.startswith("cove-")
    assert len(name) < 64


# --- message codec/schema ------------------------------------------------

def test_validate_message_valid_activate():
    action, urls = validate_message({"version": 1, "action": "activate", "urls": []})
    assert action == "activate"
    assert urls == []


def test_validate_message_valid_one_url_open():
    action, urls = validate_message({"version": 1, "action": "open", "urls": [MAGNET]})
    assert action == "open"
    assert urls == [MAGNET]


def test_validate_message_valid_multiple_url_open():
    action, urls = validate_message(
        {"version": 1, "action": "open", "urls": [MAGNET, MAGNET_2]}
    )
    assert urls == [MAGNET, MAGNET_2]


def test_validate_message_unknown_version():
    with pytest.raises(MessageError) as exc:
        validate_message({"version": 2, "action": "activate", "urls": []})
    assert exc.value.category == "unsupported_version"


def test_validate_message_unknown_action():
    with pytest.raises(MessageError) as exc:
        validate_message({"version": 1, "action": "delete", "urls": []})
    assert exc.value.category == "unknown_action"


def test_decode_payload_malformed_json():
    with pytest.raises(MessageError) as exc:
        decode_payload(b"{not json")
    assert exc.value.category == "malformed_json"


def test_decode_payload_empty_payload():
    with pytest.raises(MessageError):
        decode_payload(b"")


def test_validate_message_non_object_json():
    with pytest.raises(MessageError) as exc:
        validate_message([1, 2, 3])
    assert exc.value.category == "invalid_schema"


def test_validate_message_missing_fields():
    with pytest.raises(MessageError) as exc:
        validate_message({"version": 1, "action": "open"})
    assert exc.value.category == "invalid_schema"


def test_validate_message_wrong_field_types():
    with pytest.raises(MessageError) as exc:
        validate_message({"version": 1, "action": "open", "urls": "not-a-list"})
    assert exc.value.category == "invalid_schema"


def test_validate_message_too_many_urls():
    urls = [MAGNET] * (MAX_URLS_PER_MESSAGE + 1)
    with pytest.raises(MessageError) as exc:
        validate_message({"version": 1, "action": "open", "urls": urls})
    assert exc.value.category == "too_many_urls"


def test_validate_message_unsupported_scheme():
    with pytest.raises(MessageError) as exc:
        validate_message(
            {"version": 1, "action": "open", "urls": ["https://example.com/x"]}
        )
    assert exc.value.category == "invalid_url"


def test_validate_message_url_exceeding_magnet_maximum():
    from cove.torrent import MAX_MAGNET_LENGTH

    oversized = "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=" + "x" * MAX_MAGNET_LENGTH
    with pytest.raises(MessageError) as exc:
        validate_message({"version": 1, "action": "open", "urls": [oversized]})
    assert exc.value.category == "invalid_url"


def test_validate_message_control_characters():
    with pytest.raises(MessageError) as exc:
        validate_message(
            {"version": 1, "action": "open", "urls": [MAGNET + "\x00evil"]}
        )
    assert exc.value.category == "invalid_url"


def test_validate_message_open_requires_at_least_one_url():
    with pytest.raises(MessageError) as exc:
        validate_message({"version": 1, "action": "open", "urls": []})
    assert exc.value.category == "invalid_schema"


def test_validate_message_activate_with_one_url_rejected():
    """An `activate` request must never carry URLs - _handle_payload only
    emits open_requested for `open`, so accepting a non-empty urls list
    here would let the primary ack URLs it silently discards."""
    with pytest.raises(MessageError) as exc:
        validate_message({"version": 1, "action": "activate", "urls": [MAGNET]})
    assert exc.value.category == "invalid_schema"


def test_validate_message_activate_with_multiple_urls_rejected():
    with pytest.raises(MessageError) as exc:
        validate_message(
            {"version": 1, "action": "activate", "urls": [MAGNET, MAGNET_2]}
        )
    assert exc.value.category == "invalid_schema"


def test_encode_message_oversized_message():
    with pytest.raises(MessageError):
        encode_message({"padding": "x" * (MAX_MESSAGE_BYTES + 1)})


def test_encode_message_roundtrip_length_prefix():
    framed = encode_message({"version": 1, "ok": True, "accepted": 1})
    (length,) = struct.unpack(">I", framed[:4])
    assert length == len(framed) - 4


# --- client/server --------------------------------------------------------

def test_first_listener_becomes_primary():
    name = _unique_name()
    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True
    finally:
        server.shutdown()


def test_second_listener_cannot_become_primary():
    name = _unique_name()
    server1 = SingleInstanceServer()
    server2 = SingleInstanceServer()
    try:
        assert server1.try_become_primary(name) is True
        assert server2.try_become_primary(name) is False
    finally:
        server1.shutdown()
        server2.shutdown()


def test_concurrent_election_does_not_corrupt_the_winner():
    """Two independent processes racing try_become_primary() at nearly the
    same instant must not both end up believing they are primary, and the
    loser must never unlink the winner's freshly bound endpoint (the race
    Codex found: both could observe "nothing answered" before either had
    bound, so the second one's cleanup could unlink the first one's fresh
    socket). Real separate processes, not threads: QLocalServer/QLocalSocket
    are bound to the thread that created them, so simulating this race with
    two threads sharing one process would itself be undefined behavior.
    """
    name = _unique_name()
    script = (
        "import sys, time\n"
        "from PySide6.QtCore import QCoreApplication\n"
        "from cove.single_instance import SingleInstanceServer\n"
        "app = QCoreApplication([])\n"
        "server = SingleInstanceServer()\n"
        f"won = server.try_become_primary({name!r})\n"
        "print('PRIMARY' if won else 'SECONDARY')\n"
        "sys.stdout.flush()\n"
        "if won:\n"
        "    deadline = time.monotonic() + 2.0\n"
        "    while time.monotonic() < deadline:\n"
        "        app.processEvents()\n"
        "        time.sleep(0.01)\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outs = [p.communicate(timeout=10)[0].strip() for p in procs]
    for p in procs:
        assert p.returncode == 0
    assert sorted(outs) == ["PRIMARY", "SECONDARY"]


def test_one_magnet_forwarded_and_acknowledged():
    name = _unique_name()
    server = SingleInstanceServer()
    received = []
    server.open_requested.connect(received.append)
    try:
        assert server.try_become_primary(name) is True
        assert _forward_in_subprocess(name, [MAGNET]) is True
        assert received == [[MAGNET]]
    finally:
        server.shutdown()


def test_several_magnets_forwarded_in_order():
    name = _unique_name()
    server = SingleInstanceServer()
    received = []
    server.open_requested.connect(received.append)
    try:
        assert server.try_become_primary(name) is True
        assert _forward_in_subprocess(name, [MAGNET]) is True
        assert _forward_in_subprocess(name, [MAGNET_2]) is True
        assert received == [[MAGNET], [MAGNET_2]]
    finally:
        server.shutdown()


def test_activate_only_forwarded():
    name = _unique_name()
    server = SingleInstanceServer()
    opens = []
    activates = []
    server.open_requested.connect(opens.append)
    server.activate_requested.connect(lambda: activates.append(True))
    try:
        assert server.try_become_primary(name) is True
        assert _forward_in_subprocess(name, []) is True
        assert opens == []
        assert activates == [True]
    finally:
        server.shutdown()


# --- activate/urls acknowledgement contract ---------------------------------
#
# An `activate` message must never be acknowledged as having "accepted" URLs
# it silently discards (Codex finding: validate_message() used to allow a
# non-empty `urls` on `activate`, but _handle_payload() only ever emits
# open_requested for `action == "open"`, so those URLs vanished while the ack
# still reported them as accepted).

def test_valid_activate_with_empty_urls_succeeds():
    name = _unique_name()
    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True
        resp = _raw_ipc_roundtrip(
            name, {"version": 1, "action": "activate", "urls": []}
        )
        assert resp["ok"] is True
        assert resp["accepted"] == 0
    finally:
        server.shutdown()


def test_activate_with_one_url_is_rejected():
    name = _unique_name()
    server = SingleInstanceServer()
    opens = []
    activates = []
    server.open_requested.connect(opens.append)
    server.activate_requested.connect(lambda: activates.append(True))
    try:
        assert server.try_become_primary(name) is True
        resp = _raw_ipc_roundtrip(
            name, {"version": 1, "action": "activate", "urls": [MAGNET]}
        )
        assert resp["ok"] is False
        assert MAGNET not in resp.get("error", "")
        assert opens == []
        assert activates == []
    finally:
        server.shutdown()


def test_activate_with_multiple_urls_is_rejected():
    name = _unique_name()
    server = SingleInstanceServer()
    opens = []
    activates = []
    server.open_requested.connect(opens.append)
    server.activate_requested.connect(lambda: activates.append(True))
    try:
        assert server.try_become_primary(name) is True
        resp = _raw_ipc_roundtrip(
            name,
            {"version": 1, "action": "activate", "urls": [MAGNET, MAGNET_2]},
        )
        assert resp["ok"] is False
        assert opens == []
        assert activates == []
    finally:
        server.shutdown()


def test_rejected_activate_urls_never_reach_startup_inbox():
    """Simulates cove.app's own startup-inbox wiring (open_requested ->
    append to a buffer) against the real SingleInstanceServer/
    validate_message: a rejected activate-with-urls must never append
    anything to it, since open_requested is the only signal that path
    listens to."""
    name = _unique_name()
    server = SingleInstanceServer()
    startup_inbox: list[str] = []
    server.open_requested.connect(startup_inbox.extend)
    try:
        assert server.try_become_primary(name) is True
        resp = _raw_ipc_roundtrip(
            name, {"version": 1, "action": "activate", "urls": [MAGNET, MAGNET_2]}
        )
        assert resp["ok"] is False
        assert startup_inbox == []
    finally:
        server.shutdown()


def test_open_still_requires_at_least_one_url_end_to_end():
    name = _unique_name()
    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True
        resp = _raw_ipc_roundtrip(name, {"version": 1, "action": "open", "urls": []})
        assert resp["ok"] is False
    finally:
        server.shutdown()


def test_open_accepted_count_matches_emitted_url_count():
    name = _unique_name()
    server = SingleInstanceServer()
    received = []
    server.open_requested.connect(received.append)
    try:
        assert server.try_become_primary(name) is True
        resp = _raw_ipc_roundtrip(
            name, {"version": 1, "action": "open", "urls": [MAGNET, MAGNET_2]}
        )
        assert resp["ok"] is True
        assert resp["accepted"] == 2
        assert received == [[MAGNET, MAGNET_2]]
        assert resp["accepted"] == len(received[0])
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    "message",
    [
        {"version": 1, "action": "activate", "urls": [MAGNET]},
        {"version": 1, "action": "activate", "urls": [MAGNET, MAGNET_2]},
        {"version": 1, "action": "open", "urls": []},
        {"version": 1, "action": "bogus", "urls": [MAGNET]},
        {"version": 2, "action": "open", "urls": [MAGNET]},
    ],
)
def test_rejected_requests_never_report_more_accepted_than_emitted(message):
    """No malformed action/URL combination may produce an acknowledgement
    count larger than the number of URLs actually emitted via
    open_requested (zero, for every case here - all are rejected)."""
    name = _unique_name()
    server = SingleInstanceServer()
    received = []
    server.open_requested.connect(received.append)
    try:
        assert server.try_become_primary(name) is True
        resp = _raw_ipc_roundtrip(name, message)
        assert resp["ok"] is False
        emitted_count = sum(len(urls) for urls in received)
        assert resp.get("accepted", 0) <= emitted_count
        assert emitted_count == 0
    finally:
        server.shutdown()


def test_rejected_payload_gets_sanitized_negative_ack():
    name = _unique_name()
    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True

        script = (
            "import struct, json\n"
            "from PySide6.QtCore import QCoreApplication\n"
            "from PySide6.QtNetwork import QLocalSocket\n"
            "app = QCoreApplication([])\n"
            f"sock = QLocalSocket()\n"
            f"sock.connectToServer({name!r})\n"
            "assert sock.waitForConnected(2000)\n"
            'bad = json.dumps({"version": 99, "action": "open", "urls": []}).encode()\n'
            'sock.write(struct.pack(">I", len(bad)) + bad)\n'
            "sock.flush()\n"
            "assert sock.waitForReadyRead(2000)\n"
            "buf = bytearray(bytes(sock.readAll()))\n"
            "while len(buf) < 4:\n"
            "    assert sock.waitForReadyRead(2000)\n"
            "    buf.extend(bytes(sock.readAll()))\n"
            "(length,) = struct.unpack('>I', bytes(buf[:4]))\n"
            "del buf[:4]\n"
            "while len(buf) < length:\n"
            "    assert sock.waitForReadyRead(2000)\n"
            "    buf.extend(bytes(sock.readAll()))\n"
            "resp = json.loads(bytes(buf[:length]))\n"
            "print(json.dumps(resp))\n"
        )
        app = QCoreApplication.instance()
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        out, err = proc.communicate(timeout=2)
        assert proc.returncode == 0, err
        resp = json.loads(out.strip())
        assert resp["ok"] is False
        assert "99" not in resp["error"]
    finally:
        server.shutdown()


def test_timeout_is_bounded_when_nothing_listens():
    name = _unique_name()  # nothing bound to this name
    ok = send_to_primary(name, [MAGNET], connect_timeout_ms=200, ack_timeout_ms=200)
    assert ok is False


def test_stale_endpoint_cleanup_after_failed_connection_proof():
    from PySide6.QtNetwork import QLocalServer

    name = _unique_name()
    # Simulate a stale endpoint: bind and listen, then vanish without
    # closing gracefully (removeServer is skipped on purpose).
    ghost = QLocalServer()
    ghost.setSocketOptions(QLocalServer.UserAccessOption)
    assert ghost.listen(name)
    ghost.close()  # closes the listening socket but leaves some platforms'
    # filesystem entry behind, mimicking a crashed process.

    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True
    finally:
        server.shutdown()


def test_live_primary_endpoint_is_never_removed():
    name = _unique_name()
    live = SingleInstanceServer()
    contender = SingleInstanceServer()
    try:
        assert live.try_become_primary(name) is True
        assert contender.try_become_primary(name) is False
        # The live primary must still be reachable.
        assert _forward_in_subprocess(name, []) is True
    finally:
        live.shutdown()
        contender.shutdown()


def test_server_cleanup_removes_only_its_owned_endpoint():
    name = _unique_name()
    server = SingleInstanceServer()
    assert server.try_become_primary(name) is True
    server.shutdown()
    # Endpoint is gone: a fresh server can claim the same name immediately.
    server2 = SingleInstanceServer()
    try:
        assert server2.try_become_primary(name) is True
    finally:
        server2.shutdown()


def test_socket_closed_cleanly_after_forward():
    name = _unique_name()
    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True
        assert _forward_in_subprocess(name, [MAGNET]) is True
        # A second, independent request must still succeed - proves the
        # server accepted a fresh connection rather than being stuck.
        assert _forward_in_subprocess(name, [MAGNET_2]) is True
    finally:
        server.shutdown()


# --- accepted-connection read deadline -------------------------------------
#
# A short read timeout on the server (via SingleInstanceServer's
# connection_read_timeout_ms constructor arg) is used here so these tests
# don't have to wait out the real 5s production default.

_TEST_READ_TIMEOUT_MS = 200


def _connect_raw(name: str, timeout_ms: int = 2000):
    from PySide6.QtNetwork import QLocalSocket

    sock = QLocalSocket()
    sock.connectToServer(name)
    assert sock.waitForConnected(timeout_ms)
    return sock


def test_client_that_sends_nothing_is_disconnected():
    name = _unique_name()
    server = SingleInstanceServer(connection_read_timeout_ms=_TEST_READ_TIMEOUT_MS)
    try:
        assert server.try_become_primary(name) is True
        sock = _connect_raw(name)
        try:
            # No write at all. The server must eventually drop us rather
            # than hold the connection open forever.
            disconnected = _pump_until(
                lambda: sock.state() != sock.LocalSocketState.ConnectedState,
                timeout=3.0,
            )
            assert disconnected
        finally:
            sock.abort()
    finally:
        server.shutdown()


def test_partial_header_times_out():
    name = _unique_name()
    server = SingleInstanceServer(connection_read_timeout_ms=_TEST_READ_TIMEOUT_MS)
    try:
        assert server.try_become_primary(name) is True
        sock = _connect_raw(name)
        try:
            sock.write(b"\x00\x00")  # 2 of 4 length-prefix bytes, then stall
            sock.flush()
            disconnected = _pump_until(
                lambda: sock.state() != sock.LocalSocketState.ConnectedState,
                timeout=3.0,
            )
            assert disconnected
        finally:
            sock.abort()
    finally:
        server.shutdown()


def test_declared_but_incomplete_payload_times_out():
    name = _unique_name()
    server = SingleInstanceServer(connection_read_timeout_ms=_TEST_READ_TIMEOUT_MS)
    received = []
    server.open_requested.connect(received.append)
    try:
        assert server.try_become_primary(name) is True
        sock = _connect_raw(name)
        try:
            # Declare a 1000-byte body, then send only part of it and stall.
            sock.write(struct.pack(">I", 1000) + b"{" * 10)
            sock.flush()
            disconnected = _pump_until(
                lambda: sock.state() != sock.LocalSocketState.ConnectedState,
                timeout=3.0,
            )
            assert disconnected
            assert received == []  # never a complete/valid frame
        finally:
            sock.abort()
    finally:
        server.shutdown()


def test_completed_frame_cancels_the_deadline():
    """A connection that finishes promptly must not be touched by the
    (much longer, in real use) read-deadline machinery - proven here by
    using a deadline shorter than the round trip and confirming it still
    succeeds instead of getting cut off mid-response."""
    name = _unique_name()
    server = SingleInstanceServer(connection_read_timeout_ms=_TEST_READ_TIMEOUT_MS)
    received = []
    server.open_requested.connect(received.append)
    try:
        assert server.try_become_primary(name) is True
        assert _forward_in_subprocess(name, [MAGNET]) is True
        assert received == [[MAGNET]]
    finally:
        server.shutdown()


def test_timeout_cleanup_does_not_affect_later_valid_connections():
    name = _unique_name()
    server = SingleInstanceServer(connection_read_timeout_ms=_TEST_READ_TIMEOUT_MS)
    received = []
    server.open_requested.connect(received.append)
    try:
        assert server.try_become_primary(name) is True
        stalled = _connect_raw(name)
        try:
            _pump_until(
                lambda: stalled.state() != stalled.LocalSocketState.ConnectedState,
                timeout=3.0,
            )
        finally:
            stalled.abort()
        # A fresh, well-behaved request must still succeed afterwards.
        assert _forward_in_subprocess(name, [MAGNET]) is True
        assert received == [[MAGNET]]
    finally:
        server.shutdown()


def test_no_request_signal_emitted_for_timed_out_connection():
    name = _unique_name()
    server = SingleInstanceServer(connection_read_timeout_ms=_TEST_READ_TIMEOUT_MS)
    opens = []
    activates = []
    server.open_requested.connect(opens.append)
    server.activate_requested.connect(lambda: activates.append(True))
    try:
        assert server.try_become_primary(name) is True
        sock = _connect_raw(name)
        try:
            sock.write(struct.pack(">I", 50) + b"partial")
            sock.flush()
            _pump_until(
                lambda: sock.state() != sock.LocalSocketState.ConnectedState,
                timeout=3.0,
            )
        finally:
            sock.abort()
        assert opens == []
        assert activates == []
    finally:
        server.shutdown()


# --- browser_download IPC (fail-open browser delivery) -------------------
#
# The browser extension cancels its own download only when the native host
# reports success, so "success" must mean "the process running right now
# accepted this exact request". These tests pin that contract at the IPC
# layer: acceptance is decided by a handler installed by the running primary,
# and every unavailable/rejected/malformed path must answer ok=False.

BROWSER_URL = "https://example.invalid/dummy-file.bin"


def _browser_send_in_subprocess(name: str, payload: dict, timeout: float = 5.0) -> bool:
    """Run `send_browser_download` in a separate process while pumping this
    process's Qt event loop so the in-process server can respond."""
    app = QCoreApplication.instance()
    script = (
        "from cove.single_instance import send_browser_download\n"
        f"ok = send_browser_download({name!r}, {payload!r})\n"
        'print("OK" if ok else "FAIL")\n'
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout
    while proc.poll() is None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    out, _ = proc.communicate(timeout=2)
    return out.strip() == "OK"


def test_browser_download_reaches_handler_and_reports_success():
    name = _unique_name()
    server = SingleInstanceServer()
    seen = []
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: (seen.append(req) or True)
        ok = _browser_send_in_subprocess(
            name,
            {
                "url": BROWSER_URL,
                "filename": "dummy-file.bin",
                "cookies": "sid=dummy",
                "referrer": "https://example.invalid/page",
                "user_agent": "DummyAgent/1.0",
            },
        )
        assert ok is True
        assert len(seen) == 1
        assert seen[0]["url"] == BROWSER_URL
        assert seen[0]["filename"] == "dummy-file.bin"
        assert seen[0]["cookies"] == "sid=dummy"
        assert seen[0]["referrer"] == "https://example.invalid/page"
        assert seen[0]["user_agent"] == "DummyAgent/1.0"
    finally:
        server.shutdown()


def test_browser_download_does_not_activate_the_window():
    """An automatically captured browser download must never raise the GUI."""
    name = _unique_name()
    server = SingleInstanceServer()
    activates = []
    opens = []
    server.activate_requested.connect(lambda: activates.append(True))
    server.open_requested.connect(opens.append)
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: True
        assert _browser_send_in_subprocess(name, {"url": BROWSER_URL}) is True
        assert activates == []
        assert opens == []
    finally:
        server.shutdown()


def test_browser_download_fails_when_handler_rejects():
    name = _unique_name()
    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: False
        assert _browser_send_in_subprocess(name, {"url": BROWSER_URL}) is False
    finally:
        server.shutdown()


def test_browser_download_fails_when_no_handler_installed():
    """Before the primary is ready to accept downloads there is no handler,
    and the browser must stay responsible for its own transfer."""
    name = _unique_name()
    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True
        assert _browser_send_in_subprocess(name, {"url": BROWSER_URL}) is False
    finally:
        server.shutdown()


def test_browser_download_fails_when_handler_raises():
    name = _unique_name()
    server = SingleInstanceServer()

    def boom(req):
        raise RuntimeError("dummy failure")

    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = boom
        assert _browser_send_in_subprocess(name, {"url": BROWSER_URL}) is False
    finally:
        server.shutdown()


def test_browser_download_fails_when_no_primary_is_listening():
    assert _browser_send_in_subprocess(_unique_name(), {"url": BROWSER_URL}) is False


@pytest.mark.parametrize(
    "url",
    [
        "",
        "magnet:?xt=urn:btih:" + "a" * 40,
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://example.invalid/a\nb",
        12345,
    ],
)
def test_browser_download_rejects_unsupported_urls(url):
    name = _unique_name()
    server = SingleInstanceServer()
    calls = []
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: (calls.append(req) or True)
        resp = _raw_ipc_roundtrip(
            name, {"version": 1, "action": "browser_download", "url": url}
        )
        assert resp["ok"] is False
        assert calls == []
    finally:
        server.shutdown()


def test_browser_download_rejects_oversized_header_fields():
    name = _unique_name()
    server = SingleInstanceServer()
    calls = []
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: (calls.append(req) or True)
        resp = _raw_ipc_roundtrip(
            name,
            {
                "version": 1,
                "action": "browser_download",
                "url": BROWSER_URL,
                "cookies": "c" * (MAX_BROWSER_COOKIES_LENGTH + 1),
            },
        )
        assert resp["ok"] is False
        assert calls == []
    finally:
        server.shutdown()


def test_browser_download_strips_crlf_from_header_values():
    name = _unique_name()
    server = SingleInstanceServer()
    seen = []
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: (seen.append(req) or True)
        resp = _raw_ipc_roundtrip(
            name,
            {
                "version": 1,
                "action": "browser_download",
                "url": BROWSER_URL,
                "cookies": "sid=dummy\r\nX-Injected: yes",
                "referrer": "https://example.invalid/\rp",
                "user_agent": "Dummy\nAgent",
            },
        )
        assert resp["ok"] is True
        assert seen[0]["cookies"] == "sid=dummyX-Injected: yes"
        assert seen[0]["referrer"] == "https://example.invalid/p"
        assert seen[0]["user_agent"] == "DummyAgent"
    finally:
        server.shutdown()


def test_browser_download_response_never_echoes_the_payload():
    """Error text must be a fixed sentence - never the URL or a header the
    sender could read back (it may carry cookies or a private token)."""
    name = _unique_name()
    server = SingleInstanceServer()
    secret = "https://example.invalid/?token=dummysecrettoken"
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: False
        resp = _raw_ipc_roundtrip(
            name,
            {
                "version": 1,
                "action": "browser_download",
                "url": secret,
                "cookies": "sid=dummysecretcookie",
            },
        )
        assert resp["ok"] is False
        blob = json.dumps(resp)
        assert "dummysecrettoken" not in blob
        assert "dummysecretcookie" not in blob
    finally:
        server.shutdown()


def test_browser_download_does_not_log_url_or_headers(caplog):
    name = _unique_name()
    server = SingleInstanceServer()
    try:
        assert server.try_become_primary(name) is True
        server.browser_download_handler = lambda req: True
        with caplog.at_level("DEBUG", logger="cove.single_instance"):
            _raw_ipc_roundtrip(
                name,
                {
                    "version": 1,
                    "action": "browser_download",
                    "url": "https://example.invalid/?token=dummysecrettoken",
                    "cookies": "sid=dummysecretcookie",
                    "referrer": "https://example.invalid/dummysecretreferrer",
                    "filename": "dummysecretname.bin",
                },
            )
        text = caplog.text
        for needle in (
            "dummysecrettoken",
            "dummysecretcookie",
            "dummysecretreferrer",
            "dummysecretname",
        ):
            assert needle not in text
    finally:
        server.shutdown()


def test_browser_download_action_is_rejected_by_the_magnet_validator():
    """validate_message stays the open/activate validator; browser requests
    are routed through their own bounded validator instead."""
    with pytest.raises(MessageError):
        validate_message(
            {"version": 1, "action": "browser_download", "urls": []}
        )
