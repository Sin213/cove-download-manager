"""S07A: browser cookies stay in Cove; real HLS requests carry page origins.

These receivers exercise local HTTP only. They do not establish containment of
server-issued cookies, or behavior of other FFmpeg builds, HTTPS, or Windows.
"""

from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import signal
import subprocess
import sys
from threading import Thread
import time
from types import SimpleNamespace

import pytest

from cove.hls import ffmpeg_command


MARKER = "COVE_S07A_BROWSER_COOKIE=synthetic"
UA = "Cove-S07A-UA"
FFMPEG = shutil.which("ffmpeg")


@pytest.mark.parametrize("referrer,origin", [
    ("http://example.com/page/secret?token=SECRET_PAGE_TOKEN#frag", "http://example.com/"),
    ("HTTPS://EXAMPLE.COM:8443/page", "https://example.com:8443/"),
    ("http://user:pw@example.com:80/page", "http://example.com:80/"),
    ("https://user:pw@[2001:db8::1]:9443/path?q=secret", "https://[2001:db8::1]:9443/"),
    ("http://[::1]/page", "http://[::1]/"),
    ("http://example.com:0/path", "http://example.com:0/"),
    ("http://EXAMPLE.com:8080/p?q#f", "http://example.com:8080/"),
    ("http://127.0.0.1:80/p", "http://127.0.0.1:80/"),
    ("http://under_score.example/p", "http://under_score.example/"),
    ("http://" + "a" * 63 + ".example/p", "http://" + "a" * 63 + ".example/"),
    ("https://example.com\\SECRET_PAGE_TOKEN/page", None),
    ("https://example.com SECRET_PAGE_TOKEN/page", None),
    ("https://exa mple.com/", None),
    ("https://example.com\\\\@evil.example/", None),
    ('https://ex"ample.com/', None),
    ("https://example..com/", None),
    ("https://-.example.com/", None),
    ("https://example.com./", None),
    ("https://" + ".".join(["a" * 63] * 3 + ["a" * 62]) + "/", None),
    ("https://" + "a" * 64 + ".com/", None),
    ("http://[::1%25eth0]/", None),
    ("http://[1::2::3]/", None),
    ("http://example.com:+80/", None),
    ("http://example.com: 80/", None),
    ("http://example.com:٨٠/", None),
    ("http://example.com:８０/", None),
    ("http://example.com:/", None),
    ("http://example.com:000080/", None),
    ("http://example.com:65535/", "http://example.com:65535/"),
    ("https://example-.com/", None),
    ("https://ex<ample.com/", None),
    ("https://ex>ample.com/", None),
    ("https://ex,ample.com/", None),
    ("https://ex;ample.com/", None),
    ("https://user\u00a0name@example.com/", None),
    ("http://prefix[::1]/", None),
    ("http://[::1]suffix/", None),
    ("https://éxample.com/秘密?token=SECRET_PAGE_TOKEN", None),
    ("https://éxample.com/page", None),
    ("https://\u212a.example/private", None),
    ("https://exam\u212ale.com/p", None),
    ("https://ｅxample.com/", None),
    ("https://example.com/\u212a", "https://example.com/"),
    ("https://user\u212a:pw@example.com/", None),
    ("https://İ.example/", None),
    ("http://[fe80::1%25eth0]/p", None),
    ("http://[fe80::1%eth0]/p", None),
    ("http://exa%6dple.com/p", None),
    ("https://example.com/秘密", "https://example.com/"),
    ("http://example.com:bad/page", None),
    ("http://example.com:65536/page", None),
    ("http://[::1/page", None),
    ("http://[bad]/page", None),
    ("http:///page", None),
    ("http://", None),
    ("//example.com/page", None),
    ("example.com/page", None),
    ("ftp://example.com/page", None),
    ("javascript:alert(1)", None),
    ("file:///tmp/page", None),
    ("data:text/plain,secret", None),
    ("", None),
    (None, None),
    (b"http://example.com/page", None),
    (42, None),
])
@pytest.mark.parametrize("cookies,user_agent", [
    (MARKER, UA), ("", ""), ("session=abc", "Cove/é"),
    (MARKER + "\r\nX-Test: synthetic", "Cove\r\nUA"),
])
def test_hc03_argv_carries_only_validated_origin(referrer, origin, cookies, user_agent):
    # Mirror the production call: stored task.cookies is deliberately omitted.
    task = SimpleNamespace(cookies=cookies, referrer=referrer, user_agent=user_agent)
    cmd = ffmpeg_command(
        "https://stream.example/master.m3u8", "/tmp/out.ts",
        referrer=task.referrer, user_agent=task.user_agent,
    )
    expected = ["ffmpeg", "-n"]
    if origin is not None:
        expected += ["-headers", f"Referer: {origin}\r\n"]
    if user_agent:
        expected += ["-user_agent", user_agent]
    expected += ["-i", "https://stream.example/master.m3u8", "-c", "copy", "/tmp/out.ts"]
    assert cmd == expected
    assert task.cookies == cookies
    assert all("Cookie" not in arg and MARKER not in arg and "SECRET_PAGE_TOKEN" not in arg for arg in cmd)


@pytest.mark.parametrize("control", [chr(n) for n in range(0x20)] + [chr(0x7f)])
def test_hc03_every_control_character_omits_referrer(control):
    cmd = ffmpeg_command(
        "https://stream.example/master.m3u8", "/tmp/out.ts",
        referrer=f"https://example.com/page{control}Cookie: {MARKER}",
    )
    assert "-headers" not in cmd
    assert all(MARKER not in arg for arg in cmd)


@pytest.mark.parametrize("cookies", ["", MARKER, "session=abc\r\nX-Injected: yes"])
def test_hc03_cookie_keyword_is_rejected(cookies):
    with pytest.raises(TypeError):
        ffmpeg_command("https://stream.example/master.m3u8", "/tmp/out.ts", cookies=cookies)


@contextmanager
def _test_deadline():
    """Hard 90-second test limit on Unix; each subprocess also has a 30s cap."""
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def expired(_signum, _frame):
        raise TimeoutError("native HLS proof exceeded its 90-second deadline")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(90)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def _receivers(cross_host):
    """Fresh listeners, bound to explicit loopback addresses, with raw headers."""
    servers, threads, records = [], [], []

    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            records.append({
                "host": self.server.server_address[0], "path": self.path,
                "headers": list(self.headers.raw_items()),
            })
            status, headers, body = self.server.routes.get(self.path, (404, {}, b"missing"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *args):
            pass

    try:
        for host in ("127.0.0.1", "127.0.0.2" if cross_host else "127.0.0.1"):
            try:
                server = ThreadingHTTPServer((host, 0), Receiver)
            except OSError as exc:
                pytest.skip(f"native HLS proof cannot bind loopback receiver {host}: {exc}")
            server.daemon_threads = True
            server.routes = {}
            servers.append(server)
            thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            thread.start()
            threads.append(thread)
        yield servers, records
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive(), "HLS receiver survived cleanup"


def _run_ffmpeg(argv):
    # Ensure local requests cannot be diverted through a configured proxy.
    env = {key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")}
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=30, env=env)


def _make_hls(tmp_path, origin_b, encrypted):
    playlist = tmp_path / "variant.m3u8"
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=25", "-t", "2",
        "-c:v", "mpeg2video", "-threads", "1", "-g", "25", "-sc_threshold", "0",
        "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
        "-hls_segment_filename", str(tmp_path / "seg%d.ts"),
    ]
    if encrypted:
        key = tmp_path / "key.bin"
        key.write_bytes(bytes(range(16)))
        key_info = tmp_path / "key-info.txt"
        key_info.write_text(f"{origin_b}/key.bin\n{key}\n", encoding="utf-8")
        cmd += ["-hls_key_info_file", str(key_info)]
    result = _run_ffmpeg(cmd + [str(playlist)])
    assert result.returncode == 0, result.stderr
    assert len(list(tmp_path.glob("seg*.ts"))) == 2
    return playlist.read_text(encoding="utf-8")


def _production_launch(task, monkeypatch):
    """Run the real queue launch and command builder, replacing only QProcess.

    The returned argv is then executed by native FFmpeg. In particular, cookies
    are supplied on the real DownloadTask, rather than manually erased by tests.
    """
    from cove import queue as queue_module

    launches = []
    signal_stub = SimpleNamespace(connect=lambda _callback: None)
    process = SimpleNamespace(
        setProcessChannelMode=lambda _mode: None,
        readyReadStandardOutput=signal_stub, finished=signal_stub, errorOccurred=signal_stub,
        start=lambda program, args: launches.append([program, *args]),
    )

    class CaptureProcess:
        MergedChannels = 0

        def __new__(cls, _parent):
            return process

    monkeypatch.setattr(queue_module, "QProcess", CaptureProcess)
    queue = SimpleNamespace(
        _media_engine_source_address=lambda _task: None,
        _retire_hls_run=lambda _tid: None,
        _hls_procs={}, _hls_duration={}, _hls_stderr={}, _hls_work={},
    )
    queue_module.QueueManager._launch_hls(queue, task)
    assert len(launches) == 1
    return launches[0]


@pytest.mark.skipif(FFMPEG is None, reason="native HLS containment proof requires ffmpeg on PATH")
@pytest.mark.parametrize("cross_host,encrypted,cookies,malformed_referrer", [
    pytest.param(True, False, MARKER, False, id="HC-01-cross-host-plain"),
    pytest.param(True, True, MARKER, False, id="HC-01-cross-host-AES128"),
    pytest.param(True, False, "", False, id="HC-04-public-cross-host"),
    pytest.param(False, False, MARKER, False, id="HC-01-same-host-portable"),
    pytest.param(True, False, MARKER, True, id="HC-01-malformed-referrer"),
])
def test_native_hls_browser_credential_containment(
    tmp_path, monkeypatch, cross_host, encrypted, cookies, malformed_referrer
):
    if cross_host and not sys.platform.startswith("linux"):
        pytest.skip("second loopback host 127.0.0.2 is a Linux-only native proof")
    from cove.queue import DownloadTask

    with _test_deadline(), _receivers(cross_host) as (servers, records):
        server_a, server_b = servers
        origin_a = f"http://127.0.0.1:{server_a.server_port}"
        origin_b = f"http://{server_b.server_address[0]}:{server_b.server_port}"
        variant = _make_hls(tmp_path, origin_b, encrypted)
        variant = variant.replace("seg0.ts", f"{origin_b}/redirect.ts")
        variant = variant.replace("seg1.ts", f"{origin_b}/seg1.ts")
        server_a.routes["/master.m3u8"] = (
            200, {"Content-Type": "application/vnd.apple.mpegurl"},
            f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000\n{origin_b}/variant.m3u8\n".encode(),
        )
        server_b.routes["/variant.m3u8"] = (200, {"Content-Type": "application/vnd.apple.mpegurl"}, variant.encode())
        server_b.routes["/redirect.ts"] = (302, {"Location": f"{origin_b}/redirected.ts"}, b"")
        for route, filename in (("/redirected.ts", "seg0.ts"), ("/seg1.ts", "seg1.ts")):
            server_b.routes[route] = (200, {"Content-Type": "video/mp2t"}, (tmp_path / filename).read_bytes())
        if encrypted:
            server_b.routes["/key.bin"] = (200, {}, (tmp_path / "key.bin").read_bytes())

        # Clearly labelled positive control: prove BOTH receivers observe cookies.
        for server in servers:
            client = HTTPConnection(*server.server_address, timeout=2)
            try:
                client.request("GET", "/header-observability-control", headers={"Cookie": MARKER})
                response = client.getresponse()
                response.read()
                assert response.status == 404
            finally:
                client.close()
        controls = [r for r in records if r["path"] == "/header-observability-control"]
        assert len(controls) == 2
        assert all(("Cookie", MARKER) in r["headers"] for r in controls)

        referrer = (
            f"{origin_a}\\SECRET_PAGE_TOKEN/page" if malformed_referrer
            else f"{origin_a}/page/secret?token=SECRET_PAGE_TOKEN#frag"
        )
        task = DownloadTask(
            id=1, url=f"{origin_a}/master.m3u8", out_dir=str(tmp_path / "out"),
            filename="download.ts", backend="ffmpeg", cookies=cookies,
            referrer=referrer, user_agent=UA,
        )
        argv = _production_launch(task, monkeypatch)
        result = _run_ffmpeg(argv)
        requests = [r for r in records if r["path"] != "/header-observability-control"]
        (tmp_path / "native-proof.json").write_text(json.dumps({
            "argv": argv, "returncode": result.returncode, "requests": requests,
            "controls": controls, "stderr": result.stderr,
        }, indent=2), encoding="utf-8")
        assert result.returncode == 0, result.stderr
        assert os.path.getsize(argv[-1]) > 0
        assert task.cookies == cookies
        assert {r["path"] for r in requests} == {
            "/master.m3u8", "/variant.m3u8", "/redirect.ts", "/redirected.ts", "/seg1.ts",
        } | ({"/key.bin"} if encrypted else set())
        assert {r["host"] for r in requests} == {s.server_address[0] for s in servers}
        for request in requests:
            headers = request["headers"]
            assert MARKER not in repr(headers), request
            assert "SECRET_PAGE_TOKEN" not in repr(headers), request
            expected_referrers = [] if malformed_referrer else [f"{origin_a}/"]
            assert [v for k, v in headers if k.lower() == "referer"] == expected_referrers, request
            assert [v for k, v in headers if k.lower() == "user-agent"] == [UA], request
