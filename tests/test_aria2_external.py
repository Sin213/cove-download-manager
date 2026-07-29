"""Tests for the concurrency cap and external-download discovery."""
import base64
import threading
from unittest.mock import MagicMock, patch

import pytest

from cove.aria2 import (
    Aria2Daemon,
    Aria2Error,
    Aria2InterfaceError,
    Aria2RPC,
    MAX_CONCURRENT_DOWNLOADS,
    bittorrent_enabled,
)
from cove.config import MAX_CONNECTIONS_PER_SERVER, Settings


@pytest.fixture(autouse=True)
def _no_stale_daemon():
    """Daemon startup tests must not depend on what holds 6800 locally.

    Tests that exercise the reclaim path re-patch this inside the test, and
    the inner patch wins.
    """
    with patch.object(Aria2Daemon, "_port_in_use", return_value=False):
        yield


def _live_proc():
    """A Popen stand-in that reports a running child (poll() is None).

    A bare MagicMock returns a truthy MagicMock from poll(), which the
    daemon correctly reads as "our child already exited".
    """
    proc = MagicMock()
    proc.poll.return_value = None
    return proc


def test_daemon_lifts_max_concurrent_downloads():
    """aria2 defaults to 5; the daemon must pass an explicit higher cap."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}):
        daemon.start()

    args = popen.call_args[0][0]
    assert f"--max-concurrent-downloads={MAX_CONCURRENT_DOWNLOADS}" in args
    assert MAX_CONCURRENT_DOWNLOADS > 5


def test_daemon_caps_per_server_connections_for_stock_aria2():
    settings = Settings(connections_per_server=32)
    daemon = Aria2Daemon(settings)
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}):
        daemon.start()

    args = popen.call_args[0][0]
    assert f"--max-connection-per-server={MAX_CONNECTIONS_PER_SERVER}" in args
    assert f"--split={MAX_CONNECTIONS_PER_SERVER}" in args


def test_daemon_stops_seeding_when_the_download_completes():
    """Local BitTorrent must not keep uploading after Cove says "done"."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}):
        daemon.start()

    args = popen.call_args[0][0]
    assert "--seed-time=0" in args


def test_daemon_binds_to_the_selected_network_interface():
    settings = Settings(torrent_network_interface="wg0-mullvad")
    daemon = Aria2Daemon(settings)
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.interface_exists", return_value=True), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}):
        daemon.start()

    assert "--interface=wg0-mullvad" in popen.call_args[0][0]


def test_daemon_passes_no_interface_flag_for_any_interface():
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}):
        daemon.start()

    assert not [a for a in popen.call_args[0][0] if a.startswith("--interface=")]


def test_daemon_refuses_to_launch_when_the_interface_is_gone():
    """A VPN adapter that vanished must block traffic, not reroute it."""
    settings = Settings(torrent_network_interface="wg0-mullvad")
    daemon = Aria2Daemon(settings)
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.interface_exists", return_value=False), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}):
        with pytest.raises(Aria2InterfaceError) as excinfo:
            daemon.start()

    assert "wg0-mullvad" in str(excinfo.value)
    assert popen.call_count == 0
    # Still an Aria2Error for every existing handler, but distinguishable:
    # startup must keep the window usable so Settings can be reached, rather
    # than treating this like a missing aria2 binary.
    assert isinstance(excinfo.value, Aria2Error)


def test_daemon_reclaims_the_port_from_a_leftover_aria2():
    """A previous Cove's aria2c can outlive it and keep port 6800.

    It answers RPC with the same secret, so a naive health check passes
    while our own child is dying of a failed bind. Cove must not drive a
    process it cannot stop or restart: shut the leftover down first, then
    launch a daemon it owns.
    """
    daemon = Aria2Daemon(Settings())
    # In use before the shutdown call, free afterwards.
    in_use = iter([True, False, False])
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2Daemon, "_port_in_use", side_effect=lambda: next(in_use)), \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}), \
         patch.object(Aria2RPC, "shutdown") as shutdown:
        daemon.start()

    assert shutdown.call_count == 1
    # Exactly one spawn: the leftover is cleared before we launch, so no
    # child is ever burned on a doomed bind.
    assert popen.call_count == 1


def test_daemon_reports_a_port_it_cannot_reclaim():
    """If the leftover will not go away, say so instead of pretending."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2Daemon, "_port_in_use", return_value=True), \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}), \
         patch.object(Aria2RPC, "shutdown"):
        with pytest.raises(Aria2Error) as excinfo:
            daemon.start()

    message = str(excinfo.value)
    assert "6800" in message and "aria2c" in message
    assert popen.call_count == 0


def test_daemon_refuses_a_foreign_aria2_on_our_port():
    """A stranger's aria2 rejects our secret and must not be shut down."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()), \
         patch.object(Aria2Daemon, "_port_in_use", return_value=True), \
         patch.object(Aria2RPC, "get_version", side_effect=Aria2Error("Unauthorized")), \
         patch.object(Aria2RPC, "shutdown") as shutdown:
        with pytest.raises(Aria2Error) as excinfo:
            daemon.start()

    assert "6800" in str(excinfo.value)
    assert shutdown.call_count == 0


def test_daemon_keeps_a_healthy_child_without_shutting_anything_down():
    """The normal path must not gain a stray shutdown call."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2Daemon, "_port_in_use", return_value=False), \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}), \
         patch.object(Aria2RPC, "shutdown") as shutdown:
        daemon.start()

    assert popen.call_count == 1
    assert shutdown.call_count == 0


def test_daemon_adds_no_speculative_bittorrent_flags():
    """Only --seed-time=0 is new; DHT/PEX/ports stay on aria2 defaults."""
    daemon = Aria2Daemon(Settings())
    with patch("cove.aria2._resolve_aria2c", return_value="aria2c"), \
         patch("cove.aria2.DATA_DIR", MagicMock()), \
         patch("cove.aria2.ARIA2_SESSION", MagicMock()), \
         patch("cove.aria2.subprocess.Popen", return_value=_live_proc()) as popen, \
         patch.object(Aria2RPC, "get_version", return_value={"version": "1.37"}):
        daemon.start()

    args = " ".join(popen.call_args[0][0])
    for flag in ("--enable-dht", "--enable-peer-exchange", "--bt-",
                 "--listen-port", "--dht-listen-port", "--seed-ratio"):
        assert flag not in args


def _rpc() -> Aria2RPC:
    s = Settings()
    s.rpc_secret = "test"
    return Aria2RPC(s)


def test_tell_external_snapshot_combines_active_and_stopped():
    rpc = _rpc()
    with patch.object(rpc, "_call", side_effect=[[{"gid": "a"}], [{"gid": "b"}]]) as m:
        out = rpc.tell_external_snapshot()
    assert out == [{"gid": "a"}, {"gid": "b"}]
    methods = [c.args[0] for c in m.call_args_list]
    assert methods == ["aria2.tellActive", "aria2.tellStopped"]


def test_tell_stopped_passes_offset_num_keys():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value=[]) as m:
        rpc.tell_stopped()
    method, params = m.call_args[0]
    assert method == "aria2.tellStopped"
    assert params[0] == 0 and params[1] == 1000
    assert "gid" in params[2] and "status" in params[2]


# ---------------------------------------------------------------------------
# BitTorrent RPC surface
# ---------------------------------------------------------------------------

TORRENT_BYTES = b"d4:infod6:lengthi7e4:name5:a.bin12:piece lengthi16384eee"


def test_add_torrent_base64_encodes_the_payload():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-t") as m:
        gid = rpc.add_torrent(TORRENT_BYTES, "/dl")
    method, params = m.call_args[0]
    assert method == "aria2.addTorrent"
    assert gid == "gid-t"
    assert params[0] == base64.b64encode(TORRENT_BYTES).decode("ascii")
    assert params[1] == []          # no selective files in Slice B
    assert params[2]["dir"] == "/dl"


def test_add_torrent_never_seeds_after_completion():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-t") as m:
        rpc.add_torrent(TORRENT_BYTES, "/dl")
    assert m.call_args[0][1][2]["seed-time"] == "0"


def test_add_torrent_applies_the_speed_limit():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-t") as m:
        rpc.add_torrent(TORRENT_BYTES, "/dl", speed_limit_kbps=256)
    assert m.call_args[0][1][2]["max-download-limit"] == "256K"


def test_add_torrent_can_carry_a_forward_compatible_file_selection():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-t") as m:
        rpc.add_torrent(TORRENT_BYTES, "/dl", select_file="1,2")
    params = m.call_args[0][1]
    assert params[2]["select-file"] == "1,2"


def test_add_torrent_rejects_non_bytes_without_echoing_the_payload():
    rpc = _rpc()
    with pytest.raises(Aria2Error) as exc:
        rpc.add_torrent("not bytes", "/dl")
    assert "not bytes" not in str(exc.value)


def test_add_torrent_failure_does_not_leak_the_torrent_data():
    rpc = _rpc()
    with patch.object(rpc, "_call", side_effect=Aria2Error("RPC aria2.addTorrent failed")):
        with pytest.raises(Aria2Error) as exc:
            rpc.add_torrent(TORRENT_BYTES, "/dl")
    text = str(exc.value)
    assert base64.b64encode(TORRENT_BYTES).decode("ascii") not in text
    assert "piece length" not in text


def test_add_magnet_uses_add_uri_and_does_not_seed():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value="gid-m") as m:
        gid = rpc.add_magnet("magnet:?xt=urn:btih:" + "a" * 40, "/dl")
    method, params = m.call_args[0]
    assert method == "aria2.addUri"
    assert gid == "gid-m"
    assert params[0] == ["magnet:?xt=urn:btih:" + "a" * 40]
    assert params[1]["dir"] == "/dl"
    assert params[1]["seed-time"] == "0"


def test_tell_status_requests_the_torrent_lifecycle_keys():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value={}) as m:
        rpc.tell_status("gid-1")
    keys = m.call_args[0][1][1]
    for required in ("followedBy", "following", "infoHash", "bittorrent", "files"):
        assert required in keys
    # Existing keys must survive.
    for existing in ("gid", "status", "totalLength", "completedLength",
                     "downloadSpeed", "errorCode", "errorMessage",
                     "connections", "dir", "bitfield", "numPieces"):
        assert existing in keys


def test_external_snapshot_keys_expose_torrent_relationships():
    """The adoption guard needs these to spot a torrent child gid."""
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value=[]) as m:
        rpc.tell_active()
    keys = m.call_args[0][1][0]
    for required in ("following", "followedBy", "infoHash", "bittorrent"):
        assert required in keys


def test_get_files_returns_the_aria2_file_list():
    rpc = _rpc()
    with patch.object(rpc, "_call", return_value=[{"path": "/dl/a.bin"}]) as m:
        files = rpc.get_files("gid-1")
    assert m.call_args[0] == ("aria2.getFiles", ["gid-1"])
    assert files == [{"path": "/dl/a.bin"}]


# ---------------------------------------------------------------------------
# BitTorrent capability
# ---------------------------------------------------------------------------


def test_bittorrent_enabled_reads_the_feature_list():
    assert bittorrent_enabled({"enabledFeatures": ["Async DNS", "BitTorrent"]}) is True


def test_bittorrent_enabled_false_when_the_build_lacks_it():
    assert bittorrent_enabled({"enabledFeatures": ["Async DNS", "HTTPS"]}) is False


def test_bittorrent_enabled_false_for_a_malformed_response():
    for bad in (None, {}, {"enabledFeatures": "BitTorrent"},
                {"enabledFeatures": [None, 3]}, "BitTorrent"):
        assert bittorrent_enabled(bad) is False


def test_rpc_session_is_thread_local():
    """requests.Session isn't thread-safe; each thread must get its own."""
    rpc = _rpc()
    sessions = {}

    def grab(name):
        sessions[name] = rpc._session()

    t1 = threading.Thread(target=lambda: grab("a"))
    t2 = threading.Thread(target=lambda: grab("b"))
    t1.start(); t1.join()
    t2.start(); t2.join()
    assert sessions["a"] is not sessions["b"]
    # Same thread reuses one session.
    assert rpc._session() is rpc._session()
