import pytest

from cove.extractor import (
    FINAL_PATH_MARKER,
    is_extractor_url,
    parse_ytdlp_final_path,
    parse_ytdlp_progress,
    ytdlp_command,
)


def test_recognizes_supported_youtube_pages():
    assert is_extractor_url("https://www.youtube.com/watch?v=BMcJirSZACw")
    assert is_extractor_url("https://youtu.be/BMcJirSZACw")
    assert is_extractor_url("https://www.youtube.com/shorts/BMcJirSZACw")
    assert not is_extractor_url("https://www.youtube.com/")
    assert not is_extractor_url("https://example.com/watch?v=BMcJirSZACw")


def test_command_uses_mp4_output_template():
    command = ytdlp_command(
        "https://youtu.be/id", "/tmp/Title.%(ext)s", executable="yt-dlp"
    )
    assert command[0] == "yt-dlp"
    assert "--cookies-from-browser" not in command
    assert "--merge-output-format" in command
    assert "--no-overwrites" in command
    assert "--no-post-overwrites" in command
    assert f"after_move:{FINAL_PATH_MARKER}%(filepath)s" in command
    assert "/tmp/Title.%(ext)s" in command


def test_parses_machine_readable_final_path():
    assert parse_ytdlp_final_path(f"{FINAL_PATH_MARKER}/tmp/final.mp4") == "/tmp/final.mp4"
    assert parse_ytdlp_final_path("[download] 100% of 1.00MiB") is None


def test_command_forwards_browser_headers():
    command = ytdlp_command(
        "https://youtu.be/id",
        "/tmp/Title.%(ext)s",
        executable="yt-dlp",
        cookies="session=abc",
        referrer="https://example.com/page",
        user_agent="TestUA/1.0",
    )
    assert command[command.index("--referer") + 1] == "https://example.com/"
    assert command[command.index("--user-agent") + 1] == "TestUA/1.0"
    assert command[-1] == "https://youtu.be/id"


BIG_COOKIE = "SID=" + "dummy-cookie-data" * 400


def test_youtube_command_drops_browser_cookie_header():
    """Regression: a raw browser Cookie header made YouTube return HTTP 413."""
    url = "https://www.youtube.com/watch?v=SCD2tB1qILc"
    command = ytdlp_command(
        url,
        "/tmp/Title.%(ext)s",
        executable="yt-dlp",
        cookies=BIG_COOKIE,
        referrer=url,
        user_agent="dummy-firefox-user-agent",
    )
    assert not any("Cookie:" in arg for arg in command)
    assert not any("dummy-cookie-data" in arg for arg in command)
    assert command[command.index("--referer") + 1] == "https://www.youtube.com/"
    assert command[command.index("--user-agent") + 1] == "dummy-firefox-user-agent"
    assert command[command.index("-o") + 1] == "/tmp/Title.%(ext)s"
    assert command[command.index("-f") + 1] == "bv*[height<=1080]+ba/b[height<=1080]/b"
    assert "--merge-output-format" in command
    assert command[-1] == url


def test_all_supported_youtube_forms_drop_cookie_header():
    urls = [
        "https://www.youtube.com/watch?v=SCD2tB1qILc",
        "https://youtu.be/SCD2tB1qILc",
        "https://www.youtube.com/shorts/SCD2tB1qILc",
        "https://www.youtube.com/live/SCD2tB1qILc",
        "https://www.youtube.com/embed/SCD2tB1qILc",
        "https://m.youtube.com/watch?v=SCD2tB1qILc",
        "https://music.youtube.com/watch?v=SCD2tB1qILc",
    ]
    for url in urls:
        assert is_extractor_url(url), url
        command = ytdlp_command(
            url, "/tmp/Title.%(ext)s", executable="yt-dlp", cookies=BIG_COOKIE
        )
        assert "--add-header" not in command, url
        assert not any("dummy-cookie-data" in arg for arg in command), url


def test_building_command_does_not_mutate_the_cookie_value():
    cookies = BIG_COOKIE
    ytdlp_command(
        "https://www.youtube.com/watch?v=SCD2tB1qILc",
        "/tmp/Title.%(ext)s",
        executable="yt-dlp",
        cookies=cookies,
    )
    assert cookies == BIG_COOKIE


def test_non_youtube_extractor_url_keeps_cookie_header():
    command = ytdlp_command(
        "https://example.com/video/page",
        "/tmp/Title.%(ext)s",
        executable="yt-dlp",
        cookies="session=abc",
    )
    assert command[command.index("--add-header") + 1] == "Cookie: session=abc"


def test_command_omits_header_flags_when_absent():
    command = ytdlp_command(
        "https://youtu.be/id", "/tmp/Title.%(ext)s", executable="yt-dlp"
    )
    assert "--add-header" not in command
    assert "--referer" not in command
    assert "--user-agent" not in command


def test_parses_download_progress():
    result = parse_ytdlp_progress("[download]  42.5% of 10MiB at 2.00MiB/s ETA 00:03")
    assert result == {"percent": 42.5, "speed_bps": 2 * 1024**2}


_S08C_FULL = "https://media.example.test/account/video?id=123&token=SECRET_S08C#play"
_S08C_ORIGIN = "https://media.example.test/"
_S08C_MARKERS = ("/account", "id=123", "SECRET_S08C", "#play", "user:pw", "X-Evil")
_S08C_EDGES = [
    ('ET-01', _S08C_FULL, 'https://media.example.test/'), ('ET-02', 'https://media.example.test/', 'https://media.example.test/'), ('ET-03', 'HTTPS://MEDIA.Example.TEST:8443/p', 'https://media.example.test:8443/'), ('ET-04', 'http://example.com:80/', 'http://example.com:80/'), ('ET-05', 'https://a.test:0443/', 'https://a.test:443/'), ('ET-06', 'https://a.test:65535/', 'https://a.test:65535/'), ('ET-07', 'https://a.test:0/', 'https://a.test:0/'), ('ET-08', 'https://[2001:db8::1]:9443/p?q=1', 'https://[2001:db8::1]:9443/'), ('ET-09', 'http://[::1]/', 'http://[::1]/'), ('ET-10', 'https://user:pw@[::1]:8080/x', 'https://[::1]:8080/'), ('ET-11', 'https://user:pw@example.com/p', 'https://example.com/'), ('ET-12', 'https://user%40x@host.test/p', 'https://host.test/'), ('ET-13', 'https://example.com%2F@evil.example/', 'https://evil.example/'),
    ('ET-14', 'https://a.test?x=1', 'https://a.test/'), ('ET-15', 'https://a.test#frag', 'https://a.test/'), ('ET-16', 'https://example.com/%0d%0aX: y', 'https://example.com/'), ('ET-17', 'https://a.test/p\\q', 'https://a.test/'), ('ET-18', 'https://a.test/p q', 'https://a.test/'), ('ET-19', 'https://a.test/\xfc', 'https://a.test/'), ('ET-20', 'https://a.test/p\x85', 'https://a.test/'), ('ET-21', 'https://a.test/p\u2028', 'https://a.test/'), ('ET-22', 'https://example.com/p\xa0', 'https://example.com/'), ('ET-23', "https://example.com/" + "p" * 100000, 'https://example.com/'), ('ET-24', 'https://xn--bcher-kva.example/p', 'https://xn--bcher-kva.example/'), ('ET-25', 'http://0x7f.1/', 'http://0x7f.1/'), ('ET-26', 'http://127.1/', 'http://127.1/'),
    ('ET-27', 'https://under_score.test/', 'https://under_score.test/'), ('ET-28', "https://" + "a" * 63 + ".test/", 'https://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.test/'), ('ET-29', 'https://K.example/p', 'https://k.example/'), ('ET-30', 'HTTP://A.TEST', 'http://a.test/'), ('ET-31', 'hTtPs://a.test/', 'https://a.test/'), ('ET-32', ' https://example.com/p', "https://example.com/"), ('ET-33', 'https://example.com/p ', "https://example.com/"), ('ET-40', 'https://example.com\\@evil.example/', ''), ('ET-41', 'https://a.test\\p', ''), ('ET-42', 'https:// example.com/', ''), ('ET-43', '\xa0https://example.com/p', ''), ('ET-44', '   ', ''), ('ET-45', 'https://exa%2Fmple.test/', ''),
    ('ET-46', 'https://exa%23mple.test/', ''), ('ET-47', 'https://exa%3Ample.test/', ''), ('ET-48', 'https://example.com%40evil.test/', ''), ('ET-49', 'https://a.test%2F.evil/', ''), ('ET-50', 'https://a.test:65536/', ''), ('ET-51', 'https://a.test:-1/', ''), ('ET-52', 'https://a.test:+80/', ''), ('ET-53', 'https://a.test: 80/', ''), ('ET-54', 'https://a.test:\uff18\uff10/', ''), ('ET-55', 'https://a.test:80:80/', ''), ('ET-56', 'https://a.test:/p', ''), ('ET-57', 'http://[fe80::1%25eth0]/', ''), ('ET-58', 'https://[::1/', ''),
    ('ET-59', 'https://[::1]x/', ''), ('ET-60', 'https:///p', ''), ('ET-61', 'https://:443/', ''), ('ET-62', 'https://', ''), ('ET-63', 'http:', ''), ('ET-64', '//media.example.test/p', ''), ('ET-65', 'media.example.test/p', ''), ('ET-66', 'javascript:alert(1)', ''), ('ET-67', 'data:text/html,x', ''), ('ET-68', 'file:///etc/passwd', ''), ('ET-69', 'ftp://example.com/', ''), ('ET-70', 'https://b\xfccher.example/p', ''), ('ET-71', 'https://ex\u0430mple.com/', ''),
    ('ET-72', 'https://\uff45xample.com/', ''), ('ET-73', 'https://\u212a.example/p', ''), ('ET-74', 'https://\u212a@a.test/p', ''), ('ET-75', 'https://Ke\u212avin.example/p', ''), ('ET-76', 'https://-bad.test/', ''), ('ET-77', 'https://a..test/', ''), ('ET-78', 'https://a.test./', ''), ('ET-79', "https://" + "a" * 64 + ".test/", ''), ('ET-80', 'https://a.test/p\x00', ''), ('ET-81', 'https://a.test/p\x7f', ''), ('ET-82', 'https://a.test/p\x1f', ''), ('ET-83', '\thttps://a.test/p', ''), ('ET-84', 'https://a.test/p\n', ''),
    ('ET-85', 'https://a.test/p\r\nX-Evil: 1', ''), ('ET-86', 'https://exa\r\nmple.test/p', ''), ('ET-87', 'https://good.test/p\rX', ''), ('ET-88', 'https://good.test\r/p', ''), ('ET-89', '', ''), ('ET-90', None, ''), ('ET-91', 5, ''), ('ET-92', b'https://a.test/', ''), ('ET-93', ['https://a.test/'], ''),
]


def test_s08c_E3_executed_count(request):
    assert len(_S08C_EDGES) == len({r[0] for r in _S08C_EDGES}) == 87
    if request.config.getoption("keyword") or request.config.getoption("markexpr"): pytest.skip("Matrix collection requires an unfiltered session")
    items = [i for i in request.session.items if getattr(i, "originalname", None) == "test_s08c_A1_B1_B5_E3"]
    assert len(items) == 174
    assert {(i.callspec.params["edge_id"], i.callspec.params["source_address"]) for i in items} == {(r[0], addr) for r in _S08C_EDGES for addr in (None, "192.0.2.17")}


@pytest.mark.parametrize("source_address", [None, "192.0.2.17"], ids=["unbound", "bound"])
@pytest.mark.parametrize("edge_id,raw,expected", _S08C_EDGES, ids=[r[0] for r in _S08C_EDGES])
def test_s08c_A1_B1_B5_E3(edge_id, raw, expected, source_address):
    command = ytdlp_command("https://youtu.be/id", "/tmp/movie.%(ext)s",
                            executable="yt-dlp", referrer=raw, source_address=source_address)
    actual = command[command.index("--referer") + 1] if "--referer" in command else ""
    if edge_id in ("ET-32", "ET-33"):
        assert actual in {"", expected}
    else:
        assert actual == expected
    assert command.count("--referer") == int(bool(actual))


@pytest.mark.parametrize("raw", [_S08C_FULL, _S08C_ORIGIN], ids=["full", "origin"])
def test_s08c_A2_argument_markers(raw):
    command = ytdlp_command("https://youtu.be/id", "/tmp/movie.%(ext)s", referrer=raw)
    assert not any(marker in arg for arg in command for marker in _S08C_MARKERS)


@pytest.mark.parametrize("control", [*range(32), 127, 160])
def test_s08c_K1_controls(control):
    raw = ("\xa0https://good.test/p" if control == 160
           else "https://good.test/p" + chr(control) + "X-Evil: 1")
    command = ytdlp_command("https://youtu.be/id", "/tmp/movie.%(ext)s", referrer=raw)
    assert "--referer" not in command
    assert not any(c in arg for arg in command for c in ("\r", "\n", "X-Evil"))


@pytest.mark.parametrize("edge_id,raw,expected", _S08C_EDGES, ids=[r[0] for r in _S08C_EDGES])
def test_s08c_B7_idempotence(edge_id, raw, expected):
    from cove.hls import referrer_origin
    assert referrer_origin(referrer_origin(raw)) == referrer_origin(raw)


def test_s08c_C2_shared_helper():
    from cove import extractor, hls, queue
    assert extractor.referrer_origin is hls.referrer_origin
    assert queue.referrer_origin is hls.referrer_origin
