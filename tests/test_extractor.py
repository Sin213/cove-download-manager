from cove.extractor import (
    FINAL_PATH_MARKER,
    is_extractor_url,
    parse_ytdlp_final_path,
    parse_ytdlp_progress,
    write_cookie_jar,
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
    assert command[command.index("--referer") + 1] == "https://example.com/page"
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
    assert command[command.index("--referer") + 1] == url
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


def test_reddit_post_urls_go_to_the_extractor():
    assert is_extractor_url("https://www.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://old.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://new.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://sh.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://np.reddit.com/r/aww/comments/abc123/a_title/")
    assert is_extractor_url("https://www.reddit.com/r/aww/comments/abc123")


def test_non_post_reddit_urls_are_left_alone():
    # The feed, a subreddit and a user page are pages, not videos.
    assert not is_extractor_url("https://www.reddit.com/")
    assert not is_extractor_url("https://www.reddit.com/r/aww/")
    assert not is_extractor_url("https://www.reddit.com/r/aww")
    assert not is_extractor_url("https://www.reddit.com/user/someone/")
    # A direct media link is what the user asked for: download it as-is.
    assert not is_extractor_url("https://v.redd.it/abc123/DASH_720.mp4")
    assert not is_extractor_url("https://preview.redd.it/abc123.jpg")
    # Not Reddit at all.
    assert not is_extractor_url("https://reddit.com.evil.test/r/aww/comments/x/y/")


def test_reddit_keeps_its_cookies():
    """Reddit refuses anonymous callers, so the jar has to reach yt-dlp.

    The cookie header is dropped for extractor URLs because a full YouTube
    cookie jar makes YouTube answer with HTTP 413. That reasoning is specific
    to YouTube; Reddit has the opposite problem and fails with "Account
    authentication is required" when it is left out.
    """
    cmd = ytdlp_command(
        "https://old.reddit.com/r/sub/comments/abc123/a_title/",
        "/out/%(title)s.%(ext)s",
        executable="/usr/bin/yt-dlp",
        cookies="session=abc; token=def",
    )
    assert "--add-header" in cmd
    assert "Cookie: session=abc; token=def" in cmd


def test_youtube_still_drops_its_cookies():
    """A full jar makes YouTube answer 413, and public videos need none."""
    cmd = ytdlp_command(
        "https://www.youtube.com/watch?v=BMcJirSZACw",
        "/out/%(title)s.%(ext)s",
        executable="/usr/bin/yt-dlp",
        cookies="session=abc; token=def",
    )
    assert "--add-header" not in cmd
    assert not any("Cookie:" in part for part in cmd)


def test_a_direct_url_still_carries_its_cookies():
    cmd = ytdlp_command(
        "https://example.test/video.mp4",
        "/out/%(title)s.%(ext)s",
        executable="/usr/bin/yt-dlp",
        cookies="session=abc",
    )
    assert "Cookie: session=abc" in cmd


def test_a_cookie_jar_is_written_in_netscape_form(tmp_path):
    """Reddit's extractor reads yt-dlp's jar, not the request headers.

    `_is_logged_in` asks the cookiejar for `reddit_session`, so a header
    passed with --add-header is invisible to it however correct the header is.
    Only --cookies with a real jar file satisfies the check.
    """
    jar = tmp_path / "cookies.txt"
    written = write_cookie_jar("reddit_session=abc123; token_v2=def456", jar)

    assert written is True
    lines = [l for l in jar.read_text().splitlines() if l and not l.startswith("#")]
    assert len(lines) == 2
    for line in lines:
        fields = line.split("\t")
        assert len(fields) == 7, fields
        assert fields[0] == ".reddit.com"
        assert fields[1] == "TRUE"      # include subdomains
        assert fields[2] == "/"
        assert fields[3] == "TRUE"      # https only
    assert any(f.endswith("\treddit_session\tabc123") for f in lines)
    assert any(f.endswith("\ttoken_v2\tdef456") for f in lines)


def test_the_cookie_jar_is_not_readable_by_anyone_else(tmp_path):
    """It holds live session cookies, so it must not be world readable."""
    jar = tmp_path / "cookies.txt"
    write_cookie_jar("reddit_session=abc123", jar)
    assert oct(jar.stat().st_mode & 0o777) == "0o600"


def test_an_empty_or_malformed_cookie_string_writes_nothing(tmp_path):
    jar = tmp_path / "cookies.txt"
    assert write_cookie_jar("", jar) is False
    assert not jar.exists()
    assert write_cookie_jar("novalue; ", jar) is False
    assert not jar.exists()


def test_a_cookie_value_containing_equals_survives(tmp_path):
    # Base64 payloads end in "=" and must not be split on it.
    jar = tmp_path / "cookies.txt"
    write_cookie_jar("token=YWJjZGVm==", jar)
    line = [l for l in jar.read_text().splitlines() if not l.startswith("#")][0]
    assert line.endswith("\ttoken\tYWJjZGVm==")


def test_the_jar_is_used_instead_of_a_cookie_header(tmp_path):
    jar = tmp_path / "cookies.txt"
    cmd = ytdlp_command(
        "https://old.reddit.com/r/sub/comments/abc123/a_title/",
        "/out/%(title)s.%(ext)s",
        executable="/usr/bin/yt-dlp",
        cookies="reddit_session=abc",
        cookie_file=str(jar),
    )
    assert "--cookies" in cmd
    assert cmd[cmd.index("--cookies") + 1] == str(jar)
    # Sending both is pointless and re-triggers yt-dlp's deprecation warning.
    assert not any("Cookie:" in part for part in cmd)
