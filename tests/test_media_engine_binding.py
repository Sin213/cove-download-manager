"""Real engine proof using only generated media and loopback HTTP servers.

The server records every client's address, including redirected requests.
Process success alone is never evidence that all requests were bound.
"""

import functools
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import threading

import pytest

import cove.extractor as extractor
from cove.extractor import ytdlp_command


_PROXY_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "FTP_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "ftp_proxy",
)
_SOURCE = "127.0.0.2"
_EXACT_HTTP_FORMAT = (
    "bv*[height<=1080][protocol~='^https?$']+ba[protocol~='^https?$']/"
    "b[height<=1080][protocol~='^https?$']/b[protocol~='^https?$']"
)


def test_ytdlp_bound_production_extractor_allowlist(tmp_path, monkeypatch):
    yt_dlp = pytest.importorskip("yt_dlp", reason="yt_dlp module required for no-network extractor loading proof")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    (home / "yt-dlp.conf").write_text("--source-address 127.0.0.1\n--use-extractors generic\nhttps://example.invalid/config-url\n")
    argv = ytdlp_command("https://www.youtube.com/watch?v=fake", str(tmp_path / "out.%(ext)s"), source_address=_SOURCE)
    parsed = yt_dlp.parse_options(argv[1:-1])
    with yt_dlp.YoutubeDL(parsed.ydl_opts) as engine:
        names = {ie.IE_NAME for ie in engine._ies.values()}
    assert "generic" not in {name.lower() for name in names}
    assert names and all(name.lower().startswith("youtube") or name == "UnsupportedURL" for name in names)
    assert "UnsupportedURL" in names
    assert argv.count("--use-extractors") == 1
    assert argv[argv.index("--use-extractors") + 1] == "youtube.*,end"
    assert parsed.options.ignoreconfig is True
    assert parsed.urls == []
    assert parsed.ydl_opts["source_address"] == _SOURCE


def test_ytdlp_bound_exact_http_protocol_selector(tmp_path):
    yt_dlp = pytest.importorskip("yt_dlp", reason="yt_dlp module required for no-network format parsing proof")
    argv = ytdlp_command("https://youtu.be/fake", str(tmp_path / "out.%(ext)s"), source_address=_SOURCE)
    assert argv[argv.index("-f") + 1] == _EXACT_HTTP_FORMAT
    assert yt_dlp.parse_options(argv[1:-1]).ydl_opts["format"] == _EXACT_HTTP_FORMAT


def _fake_probe_executable(tmp_path, body):
    path = tmp_path / "fake-yt-dlp"
    path.write_text("#!/usr/bin/python3\nimport sys, time, json\n" + body + "\n")
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize("line,exit_code,expected,stream", [
    ("[debug] Request Handlers: urllib, requests, websockets, curl_cffi", 2, ("urllib", "requests", "websockets", "curl_cffi"), "stderr"),
    ("[debug] Request Handlers: urllib, websockets", 0, ("urllib", "websockets"), "stdout"),
    ("[debug] Request Handlers: requests", 7, ("requests",), "stdout"),
    ("", 0, None, "stdout"),
    ("garbage", 2, None, "stderr"),
    ("[debug] Request Handlers: ", 2, None, "stderr"),
    ("[debug] Request Handlers: , , ", 2, None, "stdout"),
])
def test_ytdlp_probe_fake_executable(tmp_path, line, exit_code, expected, stream):
    exe = _fake_probe_executable(tmp_path, f"print({line!r}, file=sys.{stream})\nsys.exit({exit_code})")
    assert extractor.ytdlp_request_handlers(exe) == expected


def test_ytdlp_bound_probe_exact_argv_and_environment(tmp_path, monkeypatch):
    recorded = tmp_path / "argv.json"
    exe = _fake_probe_executable(tmp_path, (
        "import os\n"
        f"open({str(recorded)!r}, 'w').write(json.dumps(dict(argv=sys.argv, marker=os.environ.get('COVE_TEST_PROBE'))))\n"
        "print('[debug] Request Handlers: urllib, requests', file=sys.stderr)\nsys.exit(2)"
    ))
    monkeypatch.setenv("COVE_TEST_PROBE", "known-test-value")
    real_run = subprocess.run
    calls = []

    def recording_run(argv, **kw):
        calls.append((argv, kw))
        return real_run(argv, **kw)

    monkeypatch.setattr(extractor.subprocess, "run", recording_run)
    assert extractor.ytdlp_request_handlers(exe) == ("urllib", "requests")
    record = json.loads(recorded.read_text())
    assert record["argv"] == [exe, "--ignore-config", "--no-plugin-dirs", "-v"]
    assert record["argv"][1:] == ["--ignore-config", "--no-plugin-dirs", "-v"]
    assert record["marker"] == "known-test-value"
    argv, kw = calls[0]
    assert argv == [exe, "--ignore-config", "--no-plugin-dirs", "-v"]
    assert kw == dict(stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      errors="replace", timeout=10, check=False, env=dict(os.environ))
    assert kw["env"] is not os.environ


def test_ytdlp_probe_timeout_fails_closed(tmp_path, monkeypatch):
    exe = _fake_probe_executable(tmp_path, "time.sleep(0.3)\nprint('[debug] Request Handlers: requests')")
    real_run = subprocess.run

    def short_timeout(argv, **kw):
        assert kw["timeout"] == 10
        kw["timeout"] = 0.05
        return real_run(argv, **kw)

    monkeypatch.setattr(extractor.subprocess, "run", short_timeout)
    assert extractor.ytdlp_request_handlers(exe) is None


@pytest.mark.parametrize("failure", ["missing", "not_executable"])
def test_ytdlp_probe_unavailable_executable(tmp_path, failure):
    exe = tmp_path / "unavailable-yt-dlp"
    if failure == "not_executable":
        exe.write_text("not executable")
        exe.chmod(0o600)
    assert extractor.ytdlp_request_handlers(str(exe)) is None


def _run(argv, *, env, timeout=30):
    """Kill the entire process group even if yt-dlp already exited."""
    proc = subprocess.Popen(
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
        return proc.returncode, output.decode("utf-8", errors="replace")
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)


@pytest.fixture
def native_media(tmp_path):
    ytdlp, ffmpeg = shutil.which("yt-dlp"), shutil.which("ffmpeg")
    if not ytdlp or not ffmpeg:
        pytest.skip("native binding proof needs yt-dlp and ffmpeg on PATH")
    # Test both bind and connect before deciding whether live proof is possible.
    try:
        with socket.socket() as listener, socket.socket() as client:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            client.bind((_SOURCE, 0))
            client.settimeout(2)
            client.connect(listener.getsockname())
            accepted, _ = listener.accept()
            accepted.close()
    except OSError as exc:
        pytest.skip(f"native binding proof cannot bind/connect {_SOURCE}: {exc}")
    env = os.environ.copy()
    for name in _PROXY_NAMES:
        env.pop(name, None)
    home, xdg = tmp_path / "home", tmp_path / "xdg"
    home.mkdir()
    xdg.mkdir()
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(xdg))
    media = tmp_path / "media"
    media.mkdir()

    def generate(name, options):
        argv = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
            "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-threads", "1", "-g", "10",
            *options, str(media / name),
        ]
        rc, output = _run(argv, env=env)
        assert rc == 0, (argv, rc, output)

    generate("video.mp4", ["-movflags", "+faststart"])
    generate("plain.m3u8", ["-f", "hls", "-hls_time", "1", "-hls_list_size", "0"])
    key = media / "key.bin"
    key.write_bytes(bytes(range(16)))
    key_info = media / "key-info"
    key_info.write_text(f"key.bin\n{key}\n", encoding="utf-8")
    generate("aes.m3u8", [
        "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
        "-hls_key_info_file", str(key_info),
    ])
    generate("static.mpd", ["-f", "dash", "-seg_duration", "1"])
    (media / "live.m3u8").write_text(
        (media / "plain.m3u8").read_text().replace("#EXT-X-ENDLIST\n", ""),
        encoding="utf-8",
    )
    (media / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=160x90\nplain.m3u8\n",
        encoding="utf-8",
    )
    records, lock = [], threading.Lock()
    redirect_targets = {}

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def record(self, method):
            with lock:
                records.append({
                    "source": self.client_address[0], "path": self.path,
                    "port": self.server.server_address[1], "method": method,
                })

        def do_GET(self):
            self.record("GET")
            if self.path == "/redirect.mp4":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{second.server_port}/video.mp4")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif self.path == "/ftp-redirect.mp4":
                self.send_response(302)
                self.send_header("Location", redirect_targets["ftp"])
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                super().do_GET()

        def do_HEAD(self):
            self.record("HEAD")
            super().do_HEAD()

    handler = functools.partial(Handler, directory=str(media))
    first = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    second = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (first, second)]
    for thread in threads:
        thread.start()
    try:
        yield {
            "ytdlp": ytdlp, "ffmpeg": ffmpeg, "env": env, "records": records,
            "url": f"http://127.0.0.1:{first.server_port}", "tmp": tmp_path,
            "home": home, "xdg": xdg,
            "redirect_targets": redirect_targets,
        }
    finally:
        for server in (first, second):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def _native_command(fixture, path):
    # Never duplicate binding flags here: this is the production builder.
    return ytdlp_command(
        f"{fixture['url']}/{path}", str(fixture["tmp"] / "download.%(ext)s"),
        executable=fixture["ytdlp"], source_address=_SOURCE,
    )


def _generic_fixture_argv(production):
    """For loopback generic fixtures only; leave every other argument intact."""
    assert production.count("--use-extractors") == 1
    index = production.index("--use-extractors") + 1
    assert production[index] == "youtube.*,end"
    generic = production.copy()
    generic[index] = "generic"
    assert generic[:index] + generic[index + 1:] == production[:index] + production[index + 1:]
    return generic


def _generic_native_command(fixture, path):
    return _generic_fixture_argv(_native_command(fixture, path))


def _record_proof(fixture, case, argv, rc, output, classification):
    proof = {
        "case": case, "argv": argv, "exit_code": rc,
        "source_ips": sorted({r["source"] for r in fixture["records"]}),
        "classification": classification, "requests": fixture["records"],
    }
    # Always inside pytest's external basetemp, even when an assertion fails.
    (fixture["tmp"] / "native-proof.json").write_text(json.dumps(proof, indent=2))
    (fixture["tmp"] / "native-output.txt").write_text(output)
    print("NATIVE_PROOF " + json.dumps(proof))
    assert fixture["records"], "proof must exercise the HTTP server"
    return set(proof["source_ips"])


@pytest.mark.parametrize("case,path", [
    ("progressive_redirect", "redirect.mp4"),
    ("plain_hls", "plain.m3u8"),
    ("aes_hls", "aes.m3u8"),
    ("live_hls", "live.m3u8"),
    ("static_dash", "static.mpd"),
    ("hostile_user_config", "redirect.mp4"),
])
def test_native_ytdlp_bound_or_refused(native_media, case, path):
    fixture = native_media
    if case == "hostile_user_config":
        text = (
            "--proxy http://127.0.0.1:1\n--downloader ffmpeg\n"
            "--source-address 127.0.0.1\n"
        )
        (fixture["home"] / "yt-dlp.conf").write_text(text)
        config_dir = fixture["xdg"] / "yt-dlp"
        config_dir.mkdir()
        (config_dir / "config").write_text(text)
    argv = _generic_native_command(fixture, path)
    rc, output = _run(argv, env=fixture["env"], timeout=30)
    classification = "BOUND_ONLY" if rc == 0 else "REFUSED_NO_UNBOUND"
    sources = _record_proof(fixture, case, argv, rc, output, classification)
    assert sources == {_SOURCE}, output
    if case in {"progressive_redirect", "hostile_user_config"}:
        assert rc == 0, "restricted selector must still select progressive HTTP media: " + output
        assert (fixture["tmp"] / "download.mp4").is_file()
        assert {r["port"] for r in fixture["records"]}.__len__() == 2
    elif case == "aes_hls":
        assert rc != 0, "AES HLS must be refused before ffmpeg can download segments"
        assert "Requested format is not available" in output, output
        assert all(not r["path"].endswith((".ts", "key.bin")) for r in fixture["records"])
    elif case == "live_hls":
        assert rc == 101, output
        assert all(not r["path"].endswith(".ts") for r in fixture["records"])
    elif case == "static_dash":
        assert rc != 0, "fragmented DASH must be refused by exact HTTP(S) protocol selection"
        assert "Requested format is not available" in output, output
        assert all(not r["path"].endswith((".m4s", ".mp4")) for r in fixture["records"])
    else:
        assert rc == 0 or "Requested format is not available" in output, output
        if case == "plain_hls" and rc != 0:
            assert "Requested format is not available" in output, output


def test_native_unknown_option_fails_before_network(native_media):
    fixture = native_media
    argv = _native_command(fixture, "redirect.mp4")
    argv.insert(1, "--cove-deliberately-unknown-option")
    rc, output = _run(argv, env=fixture["env"])
    assert rc == 2, output
    assert fixture["records"] == []


def test_native_ffmpeg_master_binding_does_not_bind_segments(native_media):
    fixture = native_media
    argv = [
        fixture["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostdin",
        "-local_addr", _SOURCE, "-i", f"{fixture['url']}/master.m3u8",
        "-c", "copy", "-y", str(fixture["tmp"] / "ffmpeg.mp4"),
    ]
    rc, output = _run(argv, env=fixture["env"], timeout=8)
    sources = _record_proof(fixture, "ffmpeg_local_addr_leak", argv, rc, output, "UNBOUND_DEMONSTRATED")
    assert rc == 0, output
    assert sources == {_SOURCE, "127.0.0.1"}
    assert any(r["path"] == "/master.m3u8" and r["source"] == _SOURCE for r in fixture["records"])
    assert any(r["path"].endswith(".ts") and r["source"] == "127.0.0.1" for r in fixture["records"])


@pytest.fixture
def native_ftp(native_media):
    """Recording FTP listener, rejected at login so probes finish quickly."""
    fixture = native_media
    peers = []
    stopped = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.1)
        ftp_url = f"ftp://127.0.0.1:{listener.getsockname()[1]}/x.mp4"

        def serve():
            while not stopped.is_set():
                try:
                    connection, peer = listener.accept()
                except socket.timeout:
                    continue
                peers.append(peer[0])
                with connection:
                    connection.settimeout(0.5)
                    try:
                        connection.sendall(b"220 Cove loopback FTP fixture\r\n")
                        connection.recv(4096)
                        connection.sendall(b"530 Fixture rejects FTP login\r\n")
                    except OSError:
                        pass  # A recorded peer can close before the greeting.

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        fixture["redirect_targets"]["ftp"] = ftp_url
        fixture.update(ftp_url=ftp_url, ftp_peers=peers)
        try:
            yield fixture
        finally:
            stopped.set()
            thread.join(timeout=2)
            assert not thread.is_alive(), "FTP fixture thread survived cleanup"


def _record_ftp_proof(fixture, case, argv, rc, output, classification):
    proof = {
        "case": case, "argv": argv, "exit_code": rc, "classification": classification,
        "http_source_ips": sorted({r["source"] for r in fixture["records"]}),
        "ftp_source_ips": sorted(set(fixture["ftp_peers"])),
        "requests": fixture["records"], "ftp_peers": list(fixture["ftp_peers"]),
    }
    (fixture["tmp"] / "native-proof.json").write_text(json.dumps(proof, indent=2))
    (fixture["tmp"] / "native-output.txt").write_text(output)
    print("NATIVE_PROOF " + json.dumps(proof))


def test_native_ytdlp_http_redirect_to_ftp_never_connects(native_ftp):
    fixture = native_ftp
    argv = _generic_native_command(fixture, "ftp-redirect.mp4")
    rc, output = _run(argv, env=fixture["env"], timeout=8)
    _record_ftp_proof(fixture, "http_redirect_to_ftp", argv, rc, output, "REFUSED_NO_UNBOUND")
    assert fixture["ftp_peers"] == [], "HTTP redirect opened an FTP connection"
    assert fixture["records"], "HTTP redirect was never exercised"
    assert {r["source"] for r in fixture["records"]} == {_SOURCE}, output
    assert rc != 0, output
    assert "No connection adapters were found" in output, output


def test_native_ytdlp_direct_ftp_source_address_is_unbound(native_ftp):
    fixture = native_ftp
    # Deliberately exercise the builder directly: QueueManager must gate this
    # URL before yt-dlp can perform generic extraction, ahead of format filters.
    argv = _generic_fixture_argv(ytdlp_command(
        fixture["ftp_url"], str(fixture["tmp"] / "ftp-download.%(ext)s"),
        executable=fixture["ytdlp"], source_address=_SOURCE,
    ))
    rc, output = _run(argv, env=fixture["env"], timeout=8)
    _record_ftp_proof(fixture, "direct_ftp_leak", argv, rc, output, "UNBOUND_DEMONSTRATED")
    assert fixture["ftp_peers"], "FTP probe did not exercise the listener"
    assert set(fixture["ftp_peers"]) == {"127.0.0.1"}, output
    assert fixture["records"] == []
    assert rc != 0, output


def test_native_ytdlp_requests_handler_verified(native_media):
    assert "requests" in (extractor.ytdlp_request_handlers(native_media["ytdlp"]) or ())


@pytest.mark.parametrize("generic_enabled", [False, True])
def test_native_ytdlp_meta_refresh_ftp_allowlist(native_ftp, generic_enabled):
    fixture = native_ftp
    (fixture["tmp"] / "media" / "ftp-refresh.html").write_text(
        '<html><head><title>FTP refresh fixture</title><meta http-equiv="refresh" '
        f'content="0;url={fixture["ftp_url"]}"></head><body>refresh</body></html>'
    )
    production = _native_command(fixture, "ftp-refresh.html")
    argv = _generic_fixture_argv(production) if generic_enabled else production
    rc, output = _run(argv, env=fixture["env"], timeout=8)
    _record_ftp_proof(fixture, "meta_refresh_generic" if generic_enabled else "meta_refresh_production",
                      argv, rc, output, "UNBOUND_DEMONSTRATED" if generic_enabled else "REFUSED_NO_UNBOUND")
    assert rc == 1, output
    if generic_enabled:
        assert fixture["records"], "generic extraction did not fetch the refresh page"
        assert {r["source"] for r in fixture["records"]} == {_SOURCE}
        assert fixture["ftp_peers"] and set(fixture["ftp_peers"]) == {"127.0.0.1"}
    else:
        assert fixture["records"] == []
        assert fixture["ftp_peers"] == []
        assert "Unsupported URL" in output, output


def test_native_ytdlp_dash_ftp_segments_refused(native_ftp):
    fixture = native_ftp
    ftp = fixture["ftp_url"].rsplit("/", 1)[0]
    (fixture["tmp"] / "media" / "ftp-segments.mpd").write_text(
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
        'mediaPresentationDuration="PT2S" minBufferTime="PT1S"><Period>'
        '<AdaptationSet mimeType="video/mp4" contentType="video" codecs="avc1.42c00b">'
        '<Representation id="v1" bandwidth="200000" width="160" height="90">'
        '<SegmentList timescale="1" duration="1">'
        f'<Initialization sourceURL="{ftp}/init.mp4"/>'
        f'<SegmentURL media="{ftp}/seg1.m4s"/><SegmentURL media="{ftp}/seg2.m4s"/>'
        '</SegmentList></Representation></AdaptationSet></Period></MPD>'
    )
    argv = _generic_native_command(fixture, "ftp-segments.mpd")
    rc, output = _run(argv, env=fixture["env"], timeout=8)
    _record_ftp_proof(fixture, "dash_ftp_segments", argv, rc, output, "REFUSED_NO_UNBOUND")
    assert rc != 0, output
    assert "Requested format is not available" in output, output
    assert fixture["records"]
    assert {r["source"] for r in fixture["records"]} == {_SOURCE}
    assert fixture["ftp_peers"] == []
